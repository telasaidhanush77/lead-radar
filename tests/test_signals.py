import json
from dataclasses import replace

import httpx
import pytest

from app.config import get_settings
from app.db import get_pool
from app.signals import (
    Posting,
    fetch_postings,
    is_agency,
    posting_to_lead,
    run_signals,
    signals_report,
)
from app.worker import drain


def adzuna_job(jid, company, title, desc="", area=("Deutschland", "Bayern", "München")):
    return {"id": str(jid), "title": title, "description": desc,
            "company": {"display_name": company},
            "location": {"display_name": area[-1], "area": list(area)},
            "created": "2026-11-10T08:00:00Z",
            "redirect_url": f"https://www.adzuna.de/details/{jid}"}


def adzuna(pages: dict) -> tuple[httpx.Client, list]:
    seen = []

    def handler(request):
        seen.append(request)
        key = (request.url.path, request.url.params.get("what"))
        return httpx.Response(200, json={"count": len(pages.get(key, [])),
                                         "results": pages.get(key, [])})
    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_fetch_postings_calls_adzuna_and_parses():
    client, seen = adzuna({("/v1/api/jobs/de/search/1", "TISAX"): [
        adzuna_job(1, "Müller Automotive GmbH", "Informationssicherheitsbeauftragter (TISAX)",
                   "Wir suchen ... ISMS nach TISAX")]})
    postings = fetch_postings(client, "id", "key", "de", "TISAX", days=30)
    assert postings == [Posting(id="1", country="DE", company="Müller Automotive GmbH",
                                title="Informationssicherheitsbeauftragter (TISAX)",
                                snippet="Wir suchen ... ISMS nach TISAX",
                                posted_at="2026-11-10T08:00:00Z",
                                url="https://www.adzuna.de/details/1", query="TISAX")]
    params = seen[0].url.params
    assert params["app_id"] == "id" and params["max_days_old"] == "30"
    assert params["results_per_page"] == "50"


def test_html_in_adzuna_snippet_is_stripped():
    client, _ = adzuna({("/v1/api/jobs/de/search/1", "ISMS"): [
        adzuna_job(2, "Acme", "<strong>ISMS</strong> Manager", "Build our <b>ISMS</b>")]})
    p = fetch_postings(client, "id", "key", "de", "ISMS", days=30)[0]
    assert p.title == "ISMS Manager" and p.snippet == "Build our ISMS"


@pytest.mark.parametrize("name,agency", [
    ("Hays AG", True), ("Randstad Deutschland GmbH & Co. KG", True),
    ("TechRecruit Partners", True), ("Müller Personalberatung", True),
    ("Müller Automotive GmbH", False), ("Solaris SE", False), ("", True),
])
def test_is_agency(name, agency):
    assert is_agency(name) is agency


def test_posting_to_lead_shape():
    p = Posting(id="9", country="DE", company="Müller Automotive GmbH",
                title="ISMS Manager (TISAX)", snippet="Aufbau ISMS", posted_at="2026-11-10",
                url="https://adzuna.de/9", query="TISAX")
    key, payload = posting_to_lead(p)
    assert key == "adzuna:DE:9"
    assert payload["company"] == "Müller Automotive GmbH" and payload["country"] == "DE"
    assert payload["description"].startswith("Currently hiring: ISMS Manager (TISAX).")
    assert payload["signal"] == {"source": "adzuna", "query": "TISAX",
                                 "title": "ISMS Manager (TISAX)", "url": "https://adzuna.de/9",
                                 "posted_at": "2026-11-10"}
    assert "email" not in payload and "name" not in payload  # no personal data


def test_run_signals_end_to_end(db):
    client, seen = adzuna({
        ("/v1/api/jobs/de/search/1", "TISAX"): [
            adzuna_job(1, "Müller Automotive GmbH", "Informationssicherheitsbeauftragter (TISAX)"),
            adzuna_job(2, "Hays AG", "ISMS Consultant TISAX"),
        ],
        ("/v1/api/jobs/de/search/1", "ISO 27001"): [
            adzuna_job(3, "MUELLER AUTOMOTIVE", "ISO 27001 Lead Auditor"),
            adzuna_job(1, "Müller Automotive GmbH", "Informationssicherheitsbeauftragter (TISAX)"),
        ],
    })
    settings = replace(get_settings(), adzuna_app_id="id", adzuna_app_key="key")
    summary = run_signals(db, settings, client, countries=["de"],
                          queries=["TISAX", "ISO 27001"], arbeitnow_pages=0,
                          sleep=lambda s: None)
    db.commit()
    # posting 1 matched both queries: counted once
    assert summary == {"postings": 3, "agencies_skipped": 1, "accepted": 2, "duplicates": 0}
    assert len(seen) == 2
    again = run_signals(db, settings, client, countries=["de"],
                        queries=["TISAX", "ISO 27001"], arbeitnow_pages=0,
                          sleep=lambda s: None)
    assert again["accepted"] == 0 and again["duplicates"] == 2  # re-run stores nothing
    drain(get_pool(), get_settings())
    company = db.execute("SELECT canonical_name, industry, signals FROM companies").fetchall()
    assert len(company) == 1 and company[0]["industry"] == "automotive"
    assert {"automotive_supplier", "hiring_security"} <= set(company[0]["signals"])
    report = signals_report(db)
    assert report[0]["company"] == "Müller Automotive GmbH" and report[0]["postings"] == 2
    assert report[0]["tier"] in "AB" and "tisax" in report[0]["reasons"]
    assert report[0]["example_url"].startswith("https://www.adzuna.de/details/")


def test_without_adzuna_keys_only_arbeitnow_is_called(db):
    client, seen = arbeitnow([[]])
    run_signals(db, get_settings(), client, sleep=lambda s: None)
    assert [r.url.host for r in seen] == ["www.arbeitnow.com"]


def test_one_failing_query_does_not_stop_the_run(db):
    def handler(request):
        if request.url.params.get("what") == "SOC 2":
            return httpx.Response(500)
        return httpx.Response(200, json={"count": 1, "results": [
            adzuna_job(5, "Acme Pay B.V.", "Security Officer ISO 27001",
                       area=("Nederland", "Noord-Holland", "Amsterdam"))]})

    settings = replace(get_settings(), adzuna_app_id="id", adzuna_app_key="key")
    summary = run_signals(db, settings, httpx.Client(transport=httpx.MockTransport(handler)),
                          countries=["nl"], queries=["ISO 27001", "SOC 2"],
                          arbeitnow_pages=0, sleep=lambda s: None)
    assert summary["accepted"] == 1 and summary["errors"] == ["nl/SOC 2: HTTP 500"]


def test_signals_payload_is_stored_verbatim(db):
    client, _ = adzuna({("/v1/api/jobs/de/search/1", "TISAX"): [
        adzuna_job(1, "Müller Automotive GmbH", "ISMS (TISAX)")]})
    settings = replace(get_settings(), adzuna_app_id="id", adzuna_app_key="key")
    run_signals(db, settings, client, countries=["de"], queries=["TISAX"], arbeitnow_pages=0,
                sleep=lambda s: None)
    row = db.execute("SELECT source, payload FROM raw_leads").fetchone()
    assert row["source"] == "adzuna" and row["payload"]["signal"]["query"] == "TISAX"
    json.dumps(row["payload"])


def refresh_jobs(db):
    return db.execute("SELECT status, run_after - now() AS wait FROM jobs "
                      "WHERE kind = 'signals_refresh' ORDER BY id").fetchall()


def test_schedule_refresh_is_single_and_daily(db):
    from app.signals import schedule_refresh

    assert schedule_refresh(db) is True and schedule_refresh(db) is False  # restarts: one job
    assert len(refresh_jobs(db)) == 1 and refresh_jobs(db)[0]["wait"].total_seconds() <= 1
    db.execute("UPDATE jobs SET status = 'done', updated_at = now()")
    schedule_refresh(db)  # a run finished just now: next one tomorrow, not straight away
    assert refresh_jobs(db)[-1]["wait"].total_seconds() > 23 * 3600


def test_refresh_handler_runs_and_reschedules(db, monkeypatch):
    from app import signals

    calls = []
    monkeypatch.setattr(signals, "run_signals",
                        lambda conn, settings, client: calls.append(1) or {"accepted": 0})
    signals.signals_refresh(db, {}, get_settings())  # no keys needed: Arbeitnow is open
    assert calls == [1]
    (job,) = refresh_jobs(db)
    assert job["status"] == "queued" and job["wait"].total_seconds() > 23 * 3600




def test_worker_knows_the_refresh_job():
    from app.worker import HANDLERS

    assert "signals_refresh" in HANDLERS


def arbeitnow_job(slug, company, title, location="München", desc="<p>Wir bauen ein ISMS</p>"):
    from html import escape

    return {"slug": slug, "company_name": company, "title": title, "description": escape(desc),
            "location": location, "url": f"https://www.arbeitnow.com/jobs/companies/x/{slug}",
            "created_at": 1795060514, "remote": False, "tags": [], "job_types": []}


def arbeitnow(pages: list[list]) -> tuple[httpx.Client, list]:
    seen = []

    def handler(request):
        seen.append(request)
        n = int(request.url.params.get("page", "1"))
        nxt = (f"https://www.arbeitnow.com/api/job-board-api?page={n + 1}"
               if n < len(pages) else None)
        return httpx.Response(200, json={"data": pages[n - 1], "links": {"next": nxt}})
    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_arbeitnow_keeps_only_security_roles_and_parses():
    from app.signals import fetch_arbeitnow

    client, seen = arbeitnow([[
        arbeitnow_job("a-1", "Müller Automotive GmbH",
                      "Informationssicherheitsbeauftragter (TISAX)"),
        arbeitnow_job("a-2", "Trading212", "Deputy Head of Compliance", "London"),
        arbeitnow_job("a-3", "Shark", "Senior Marketing Manager", "Frankfurt am Main"),
    ], [arbeitnow_job("a-4", "Acme Pay B.V.", "ISO 27001 Security Officer", "Amsterdam")]])
    postings, errors = fetch_arbeitnow(client, pages=5, sleep=lambda s: None)
    assert errors == [] and len(seen) == 2  # follows `next`, stops when it is null
    assert [(p.company, p.country, p.query) for p in postings] == [
        ("Müller Automotive GmbH", "DE", "TISAX"), ("Acme Pay B.V.", "NL", "ISO 27001")]
    p = postings[0]
    assert p.id == "a-1" and p.snippet == "Wir bauen ein ISMS" and p.url.startswith("https://")
    assert p.posted_at.startswith("2026-")
    assert posting_to_lead(p)[0] == "arbeitnow:a-1"
    assert posting_to_lead(p)[1]["signal"]["source"] == "arbeitnow"


@pytest.mark.parametrize("location,country", [
    ("München", "DE"), ("Berlin, Germany", "DE"), ("London", "GB"), ("Zürich", "CH"),
    ("Paris", "FR"), ("Wien", "AT"), ("Remote", None), ("", None),
])
def test_country_from_location(location, country):
    from app.signals import country_from_location

    assert country_from_location(location) == country


def test_arbeitnow_end_to_end_and_page_cap(db):
    client, seen = arbeitnow([[arbeitnow_job("a-1", "Müller Automotive GmbH",
                                             "ISMS Manager (TISAX)")]] * 5)
    summary = run_signals(db, get_settings(), client, arbeitnow_pages=3, sleep=lambda s: None)
    assert len(seen) == 3 and summary["accepted"] == 1 and summary["postings"] == 1
    db.commit()
    drain(get_pool(), get_settings())
    (row,) = signals_report(db)
    assert row["company"] == "Müller Automotive GmbH" and "tisax" in row["reasons"]


@pytest.mark.parametrize("title,label", [
    ("Business Development Representative - Cybersecurity", None),
    ("Account Executive, Security", None), ("Security Engineer", "Information security"),
    ("IT Security Officer (m/w/d)", "Information security"),
])
def test_sellers_of_security_are_not_buyers(title, label):
    from app.signals import role_label

    assert role_label(title) == label


def test_same_remote_role_in_two_countries_counts_once(db):
    client, _ = arbeitnow([[
        arbeitnow_job("h-1", "Happyrobot.ai", "Senior Application Security Engineer", "London"),
        arbeitnow_job("h-2", "Happyrobot.ai", "Senior Application Security Engineer", "France"),
    ]])
    summary = run_signals(db, get_settings(), client, sleep=lambda s: None)
    assert summary["postings"] == 1 and summary["accepted"] == 1
