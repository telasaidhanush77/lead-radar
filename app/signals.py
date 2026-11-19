"""Real buying signals: European companies hiring for compliance roles right now.

A company advertising an "ISMS Manager (ISO 27001)" or a "TISAX coordinator" is building
the exact programme a compliance-automation vendor sells, and it says so publicly. This
module pulls those postings from the Arbeitnow job board API (official, free, no key; mostly
DACH) and, when keys are set, the Adzuna API, drops recruiting agencies, and feeds each
posting into the normal pipeline as a lead.

    python -m app.signals            # pull, process, print the report
"""

import datetime
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from html import unescape

import httpx
import psycopg

from app.agent import html_to_text
from app.config import Settings
from app.ingest import store_lead
from app.normalize import fold

log = logging.getLogger("signals")

ADZUNA_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
# Adzuna country codes; DACH first, then the rest of Secfix's European market.
COUNTRIES = ["de", "at", "ch", "nl", "be", "fr", "it", "es", "pl", "gb"]
QUERIES = ["TISAX", "ISO 27001", "SOC 2", "ISMS"]
DAYS = 30
PER_PAGE = 50
PACE_S = 2.5  # free tier is rate-limited per minute; be a polite client

ARBEITNOW_URL = "https://www.arbeitnow.com/api/job-board-api"
ARBEITNOW_PAGES = 15  # 300 jobs a page, newest first: the last ~4,500 postings
ARBEITNOW_PACE_S = 2.0  # "free public API, please do not abuse"; 1 s hit 429 at page 18
# Arbeitnow has no search: keep roles whose *title* says they build security/compliance.
# Ordered: the first label that matches is recorded as the posting's query.
ROLE_PATTERNS = [
    ("TISAX", r"tisax"),
    ("ISO 27001", r"iso ?/? ?(iec)? ?27001"),
    ("SOC 2", r"soc ?2"),
    ("ISMS", r"\bisms\b"),
    ("Information security", r"informationssicherheit|information security|infosec|"
                             r"it-sicherheit|cyber ?security|\bciso\b|security officer|"
                             r"security (manager|engineer|lead|analyst)|sicherheitsbeauftragte"),
    ("Data protection", r"datenschutz|data protection officer|\bdpo\b"),
]
# A security *vendor* hiring sales staff sells security; it is not a buyer.
NOT_BUYER_ROLES = re.compile(r"sales|business development|account (executive|manager)|"
                             r"marketing|recruit|presales|pre-sales|customer success")
# Arbeitnow gives a free-text location. Folded substrings -> ISO country.
LOCATIONS = {
    "DE": ("germany", "deutschland", "berlin", "muenchen", "munich", "hamburg", "frankfurt",
           "koeln", "cologne", "stuttgart", "duesseldorf", "leipzig", "dresden", "nuernberg",
           "hannover", "bremen", "essen", "dortmund", "karlsruhe", "mannheim", "bonn", "bayern",
           "bavaria", "wolfsburg", "ingolstadt", "regensburg"),
    "AT": ("austria", "oesterreich", "wien", "vienna", "graz", "linz", "salzburg"),
    "CH": ("switzerland", "schweiz", "zuerich", "zurich", "geneva", "genf", "basel", "bern",
           "lausanne"),
    "GB": ("united kingdom", "london", "manchester", "bristol", "england", "edinburgh"),
    "FR": ("france", "paris", "lyon", "toulouse"), "NL": ("netherlands", "amsterdam",
           "rotterdam", "utrecht", "eindhoven"), "ES": ("spain", "madrid", "barcelona"),
    "IT": ("italy", "milan", "milano", "rome", "roma"), "PL": ("poland", "warsaw", "krakow"),
    "IE": ("ireland", "dublin"), "BE": ("belgium", "brussels"), "PT": ("portugal", "lisbon"),
    "SE": ("sweden", "stockholm"), "DK": ("denmark", "copenhagen"),
}

# Agencies post on behalf of unnamed clients: the company behind the role is unknown.
AGENCY_NAMES = {"hays", "randstad", "adecco", "manpower", "michael page", "robert half",
                "page personnel", "gulp", "experis", "allgeier", "brunel", "jobot",
                "harvey nash", "computer futures", "amoria bond", "frank group"}
AGENCY_WORDS = re.compile(r"personal(berat|dienst|vermittl|service)|recruit|staffing|"
                          r"headhunt|zeitarbeit|arbeitnehmerueberlassung|talent")


@dataclass(frozen=True)
class Posting:
    id: str
    country: str
    company: str
    title: str
    snippet: str
    posted_at: str
    url: str
    query: str
    source: str = "adzuna"


def is_agency(company: str) -> bool:
    name = fold(company or "")
    return not name.strip() or any(a in name for a in AGENCY_NAMES) or bool(
        AGENCY_WORDS.search(name))


def fetch_postings(client: httpx.Client, app_id: str, app_key: str, country: str,
                   query: str, days: int = DAYS) -> list[Posting]:
    response = client.get(
        ADZUNA_URL.format(country=country, page=1), timeout=30,
        params={"app_id": app_id, "app_key": app_key, "what": query,
                "max_days_old": days, "results_per_page": PER_PAGE,
                "content-type": "application/json"})
    response.raise_for_status()
    return [
        Posting(id=str(job["id"]), country=country.upper(),
                company=(job.get("company") or {}).get("display_name", "").strip(),
                title=html_to_text(job.get("title", "")),
                snippet=html_to_text(job.get("description", "")),
                posted_at=job.get("created", ""), url=job.get("redirect_url", ""),
                query=query)
        for job in response.json().get("results", [])
    ]


def role_label(title: str) -> str | None:
    t = fold(title)
    if NOT_BUYER_ROLES.search(t):
        return None
    return next((label for label, rx in ROLE_PATTERNS if re.search(rx, t)), None)


def country_from_location(location: str | None) -> str | None:
    loc = fold(location or "")
    return next((cc for cc, words in LOCATIONS.items() if any(w in loc for w in words)), None)


def fetch_arbeitnow(client: httpx.Client, pages: int = ARBEITNOW_PAGES,
                    sleep: Callable[[float], None] = time.sleep) -> tuple[list[Posting], list]:
    """Walk the newest `pages` pages; keep security/compliance roles. (postings, errors)."""
    postings, errors, url = [], [], ARBEITNOW_URL
    for n in range(1, pages + 1):
        if n > 1:
            sleep(ARBEITNOW_PACE_S)
        try:
            response = client.get(url, params={"page": n}, timeout=30)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            errors.append(f"arbeitnow/page {n}: {exc}")
            break
        for job in body.get("data", []):
            label = role_label(job.get("title", ""))
            if label is None:
                continue
            ts = job.get("created_at")
            postings.append(Posting(
                id=str(job["slug"]), country=country_from_location(job.get("location")),
                company=(job.get("company_name") or "").strip(),
                title=html_to_text(job.get("title", "")),
                snippet=html_to_text(unescape(job.get("description", "")))[:500],
                posted_at=(datetime.datetime.fromtimestamp(int(ts), datetime.UTC).isoformat()
                           if ts else ""),
                url=job.get("url", ""), query=label, source="arbeitnow"))
        if not (body.get("links") or {}).get("next"):
            break
    return postings, errors


def posting_to_lead(p: Posting) -> tuple[str, dict]:
    """(idempotency key, webhook-style payload). The posting is the evidence, kept verbatim
    under `signal`; there is deliberately no person in it, only the company."""
    key = f"adzuna:{p.country}:{p.id}" if p.source == "adzuna" else f"{p.source}:{p.id}"
    return key, {
        "company": p.company,
        "country": p.country,
        "description": f"Currently hiring: {p.title}. {p.snippet}",
        "signal": {"source": p.source, "query": p.query, "title": p.title, "url": p.url,
                   "posted_at": p.posted_at},
    }


def run_signals(conn: psycopg.Connection, settings: Settings, client: httpx.Client,
                countries: list[str] = COUNTRIES, queries: list[str] = QUERIES,
                arbeitnow_pages: int = ARBEITNOW_PAGES,
                sleep: Callable[[float], None] = time.sleep) -> dict:
    """Pull every source, store new postings as leads. The worker does the rest."""
    postings, errors = fetch_arbeitnow(client, arbeitnow_pages, sleep) if arbeitnow_pages \
        else ([], [])
    if settings.adzuna_app_id and settings.adzuna_app_key:
        for i, (country, query) in enumerate((c, q) for c in countries for q in queries):
            if i or postings:
                sleep(PACE_S)
            try:
                postings += fetch_postings(client, settings.adzuna_app_id,
                                           settings.adzuna_app_key, country, query)
            except httpx.HTTPError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                errors.append(f"{country}/{query}: " + (f"HTTP {status}" if status else str(exc)))
    unique = list({(p.source, p.country, p.id): p for p in postings}.values())  # one per posting
    # The same remote role is often listed once per country: keep the first listing.
    unique = list({(fold(p.company), fold(p.title)): p for p in reversed(unique)}.values())[::-1]
    real = [p for p in unique if not is_agency(p.company)]
    accepted = sum(store_lead(conn, p.source, *posting_to_lead(p)) is not None for p in real)
    summary = {"postings": len(unique), "agencies_skipped": len(unique) - len(real),
               "accepted": accepted, "duplicates": len(real) - accepted}
    if errors:
        summary["errors"] = errors
    log.info("signals pulled", extra=summary)
    return summary


REFRESH_EVERY = "24 hours"


def schedule_refresh(conn: psycopg.Connection, from_now: bool = False) -> bool:
    """Queue the next daily pull unless one is already queued. At startup the next run is due
    24 h after the last finished one, so a free-tier host that sleeps and wakes all day does not
    re-pull on every wake. Returns whether a job was added."""
    return conn.execute(
        f"""
        INSERT INTO jobs (kind, payload, run_after)
        SELECT 'signals_refresh', '{{}}', CASE WHEN %s THEN now() + interval '{REFRESH_EVERY}'
            ELSE greatest(now(), coalesce(max(updated_at) + interval '{REFRESH_EVERY}', now()))
            END
        FROM jobs WHERE kind = 'signals_refresh' AND status = 'done'
        ON CONFLICT (kind) WHERE kind = 'signals_refresh' AND status = 'queued' DO NOTHING
        """, (from_now,)).rowcount == 1


def signals_refresh(conn: psycopg.Connection, payload: dict, settings: Settings) -> None:
    """Job handler: pull today's postings, then book tomorrow's run."""
    # ponytail: the pull (~40 paced calls, ~2 min) runs inside the job's transaction; fine
    # under the 5-minute stale-job reclaim, split per country if the query list grows.
    with httpx.Client() as client:
        run_signals(conn, settings, client)
    schedule_refresh(conn, from_now=True)


def signals_report(conn: psycopg.Connection, limit: int = 15) -> list[dict]:
    """Companies with live hiring signals, best first: how many postings, score, why."""
    return conn.execute(
        """
        SELECT c.id, c.canonical_name AS company, c.country, s.total AS score, s.tier,
               (SELECT string_agg(k || ' +' || v, ', ' ORDER BY v::int DESC, k)
                FROM jsonb_each_text(s.breakdown) AS b(k, v)) AS reasons,
               count(r.id) AS postings,
               (array_agg(r.payload -> 'signal' ->> 'title' ORDER BY r.id))[1] AS example_title,
               (array_agg(r.payload -> 'signal' ->> 'url' ORDER BY r.id))[1] AS example_url
        FROM raw_leads r
        JOIN contacts ct ON ct.id = r.contact_id
        JOIN companies c ON c.id = ct.company_id
        JOIN scores s ON s.company_id = c.id
        WHERE r.source IN ('adzuna', 'arbeitnow')
        GROUP BY c.id, s.total, s.tier, s.breakdown
        ORDER BY s.total DESC, count(r.id) DESC, c.canonical_name
        LIMIT %s
        """, (limit,)).fetchall()


def report_markdown(summary: dict, rows: list[dict], when: str) -> str:
    lines = [
        f"Pulled {when} from the [Arbeitnow](https://www.arbeitnow.com) job board API: "
        f"**{summary['postings']}** open security/compliance roles "
        f"({', '.join(label for label, _ in ROLE_PATTERNS)}) among the newest "
        f"{ARBEITNOW_PAGES * 300:,} postings; {summary['agencies_skipped']} from recruiting "
        f"agencies dropped.",
        "",
        "| Company | Country | Score | Tier | Postings | Why | Example role |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        title = (r["example_title"] or "").replace("|", "/")[:60]
        lines.append(f"| {r['company']} | {r['country']} | {r['score']} | {r['tier']} | "
                     f"{r['postings']} | {r['reasons'] or '—'} | [{title}]({r['example_url']}) |")
    return "\n".join(lines)


if __name__ == "__main__":
    import datetime
    from pathlib import Path

    from app.config import get_settings
    from app.db import get_pool, migrate
    from app.log import setup_logging
    from app.worker import drain
    from bench.run import load_dotenv

    load_dotenv()
    get_settings.cache_clear()
    setup_logging()
    settings = get_settings()
    pool = get_pool()
    with pool.connection() as conn:
        migrate(conn)
    with pool.connection() as conn, httpx.Client() as client:
        summary = run_signals(conn, settings, client)
    drain(pool, settings)
    with pool.connection() as conn:
        rows = signals_report(conn)
    table = report_markdown(summary, rows, datetime.date.today().isoformat())
    readme = Path(__file__).resolve().parent.parent / "README.md"
    text = readme.read_text(encoding="utf-8")
    start, end = "<!-- signals:start -->", "<!-- signals:end -->"
    if start in text:
        head, _, rest = text.partition(start)
        readme.write_text(f"{head}{start}\n{table}\n{end}{rest.partition(end)[2]}",
                          encoding="utf-8")
    pool.close()
    print(summary)
    print(table)
