"""One company, everything a rep needs to act: score and why, the public evidence, CRM state,
the approved brief. Public by design, so it carries no names or email addresses."""

import psycopg

from app.dashboard import esc


def company_profile(conn: psycopg.Connection, company_id: int) -> dict | None:
    c = conn.execute("SELECT id, canonical_name AS name, country, employees, industry, owner, "
                     "route, signals FROM companies WHERE id = %s", (company_id,)).fetchone()
    if c is None:
        return None
    one = lambda sql: conn.execute(sql, (company_id,)).fetchone()  # noqa: E731
    evidence = conn.execute(
        "SELECT r.payload -> 'signal' ->> 'title' AS title, r.payload -> 'signal' ->> 'url' AS url,"
        " r.payload -> 'signal' ->> 'posted_at' AS posted_at,"
        " r.payload -> 'signal' ->> 'query' AS query "
        "FROM raw_leads r JOIN contacts ct ON ct.id = r.contact_id "
        "WHERE ct.company_id = %s AND r.payload ? 'signal' ORDER BY posted_at DESC, r.id",
        (company_id,)).fetchall()
    contacts = conn.execute(
        "SELECT title, seniority FROM contacts WHERE company_id = %s AND title IS NOT NULL "
        "ORDER BY id", (company_id,)).fetchall()
    return c | {
        "score": one("SELECT total, tier, breakdown, model_version FROM scores "
                     "WHERE company_id = %s"),
        "evidence": evidence,
        "contacts": contacts,
        "crm": one("SELECT external_id, synced_at FROM crm_sync WHERE company_id = %s"),
        "brief": one("SELECT brief, email_draft AS email, mode, approved_at FROM briefs "
                     "WHERE company_id = %s AND status = 'approved' "
                     "ORDER BY approved_at DESC LIMIT 1"),
    }


def _link(url: str | None, text: str | None) -> str:
    return (f'<a href="{esc(url)}" rel="noopener nofollow" target="_blank">{esc(text)}</a>'
            if (url or "").startswith("https://") else esc(text))


def render_profile(p: dict) -> str:
    s = p["score"] or {"total": "—", "tier": "—", "breakdown": {}}
    chips = "".join(f'<span class="chip">{esc(k)} <b>+{esc(v)}</b></span>'
                    for k, v in s["breakdown"].items()) or '<span class="meta">nothing fired</span>'
    evidence = "".join(f'<li>{_link(e["url"], e["title"])} <span class="meta">'
                       f'{esc((e["posted_at"] or "")[:10])} · matched “{esc(e["query"])}”</span></li>'
                       for e in p["evidence"]) or '<li class="meta">No public postings.</li>'
    people = "".join(f'<li>{esc(x["title"])} <span class="meta">{esc(x["seniority"])}</span></li>'
                     for x in p["contacts"]) or '<li class="meta">No contacts yet.</li>'
    crm = (f'synced as <span class="mono">{esc(p["crm"]["external_id"])}</span>'
           if p["crm"] else "not synced yet")
    brief = (f'<pre>{esc(p["brief"]["brief"])}</pre>' if p["brief"]
             else '<p class="meta">No approved brief.</p>')
    facts = " · ".join(esc(x) for x in (p["country"], p["industry"],
                                         p["employees"] and f'{p["employees"]} staff',
                                         p["owner"] and f'owner {p["owner"]}') if x)
    return PROFILE.format(name=esc(p["name"]), facts=facts, total=esc(s["total"]),
                          tier=esc(s["tier"]), chips=chips, evidence=evidence, people=people,
                          crm=crm, brief=brief, id=esc(p["id"]))


PROFILE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} · Lead Radar</title>
<style>
:root {{ --bg:#f4f3ef; --surface:#fff; --ink:#121212; --muted:#6b6a66; --line:#dcdad3; --a:#ff4f00;
  --sans:"IBM Plex Sans",system-ui,sans-serif; --mono:"IBM Plex Mono",ui-monospace,monospace; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#111110; --surface:#1a1a19; --ink:#eeeee8;
  --muted:#9a998f; --line:#2e2e2b; }} }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 var(--sans); }}
main {{ max-width:860px; margin:0 auto; padding:28px 16px 64px; }}
a {{ color:var(--ink); }} .meta {{ color:var(--muted); font-size:13px; }}
h1 {{ font-size:clamp(26px,5vw,40px); margin:8px 0 4px; letter-spacing:-.02em; }}
.score {{ display:flex; gap:14px; align-items:baseline; margin:18px 0; }}
.score b {{ font:600 44px var(--mono); color:var(--a); }}
section {{ background:var(--surface); border:1px solid var(--line); padding:14px 16px; margin:12px 0; }}
h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.08em; margin:0 0 8px; }}
ul {{ margin:0; padding-left:18px; }} li {{ margin:4px 0; }}
.chip {{ display:inline-block; border:1px solid var(--line); padding:2px 8px; margin:2px; font-size:13px; }}
pre {{ white-space:pre-wrap; font:14px/1.5 var(--sans); margin:0; }}
.mono {{ font-family:var(--mono); }}
</style></head><body><main>
<a href="/" class="meta">← all leads</a>
<h1>{name}</h1><div class="meta">{facts}</div>
<div class="score"><b>{total}</b><span>tier {tier}</span></div>
<section><h2>Why this score</h2>{chips}</section>
<section><h2>Public evidence · open roles</h2><ul>{evidence}</ul></section>
<section><h2>Known roles at the company</h2><ul>{people}</ul></section>
<section><h2>CRM</h2>{crm}</section>
<section><h2>Approved AE brief</h2>{brief}</section>
<p class="meta">JSON: <a href="/api/companies/{id}">/api/companies/{id}</a></p>
</main></body></html>"""
