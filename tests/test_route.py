from dataclasses import replace

import httpx
import pytest

from app import route
from app.config import get_settings
from app.db import get_pool
from app.queue import enqueue
from app.worker import drain


def add_company(db, country="DE", tier="A", total=90):
    cid = db.execute("INSERT INTO companies (canonical_name, country) VALUES ('Acme', %s) "
                     "RETURNING id", (country,)).fetchone()["id"]
    db.execute("INSERT INTO scores (company_id, total, tier, breakdown, model_version) "
               "VALUES (%s, %s, %s, '{\"tisax\": 30}', 'rules-v1')", (cid, total, tier))
    return cid


@pytest.mark.parametrize("country,region", [("DE", "DACH"), ("CH", "DACH"), ("NL", "Benelux"),
                                            ("FI", "Nordics"), ("FR", "Rest"), (None, "Rest")])
def test_region_for(country, region):
    assert route.region_for(country) == region


def test_route_for_tier():
    assert [route.route_for(t) for t in "ABC"] == ["ae", "bdr", "nurture"]


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(route, "send_alert", lambda settings, company, score: sent.append(
        company["id"]))
    return sent


def test_route_sets_route_owner_and_queues_crm(db, alerts):
    cid = add_company(db, tier="B")
    route.route_company(db, {"company_id": cid}, get_settings())
    assert db.execute("SELECT route, owner FROM companies").fetchone() == {
        "route": "bdr", "owner": "anna"}
    assert db.execute("SELECT kind FROM jobs").fetchone() == {"kind": "crm_sync"}
    assert alerts == []


def test_owner_is_sticky(db, alerts):
    cid = add_company(db, country="DE")
    route.route_company(db, {"company_id": cid}, get_settings())
    db.execute("UPDATE companies SET country = 'GB'")
    route.route_company(db, {"company_id": cid}, get_settings())
    assert db.execute("SELECT owner FROM companies").fetchone()["owner"] == "anna"


def test_round_robin_balances(db, alerts):
    for _ in range(4):
        route.route_company(db, {"company_id": add_company(db, tier="C")}, get_settings())
    owners = [r["owner"] for r in db.execute("SELECT owner FROM companies ORDER BY id")]
    assert owners == ["anna", "jonas", "anna", "jonas"]


def test_alert_fires_once(db, alerts):
    cid = add_company(db, tier="A")
    for _ in range(3):
        route.route_company(db, {"company_id": cid}, get_settings())
        route.alert_company(db, {"company_id": cid}, get_settings())
    assert alerts == [cid]
    assert db.execute("SELECT alerted_at IS NOT NULL AS ok FROM companies").fetchone()["ok"]


def test_tier_b_no_alert(db, alerts):
    route.route_company(db, {"company_id": add_company(db, tier="B")}, get_settings())
    assert alerts == []
    assert db.execute("SELECT count(*) AS n FROM jobs WHERE kind = 'alert'").fetchone()["n"] == 0


def test_tier_a_queues_alert_and_crm(db, alerts):
    route.route_company(db, {"company_id": add_company(db, tier="A")}, get_settings())
    kinds = sorted(r["kind"] for r in db.execute("SELECT kind FROM jobs"))
    assert kinds == ["alert", "crm_sync"] and alerts == []


def test_slack_payload(db, monkeypatch):
    seen = []
    monkeypatch.setattr(route, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(
        lambda req: seen.append(req) or httpx.Response(200))))
    settings = replace(get_settings(), slack_webhook_url="https://hooks.slack.test/x")
    cid = add_company(db)
    route.route_company(db, {"company_id": cid}, settings)
    route.alert_company(db, {"company_id": cid}, settings)
    assert len(seen) == 1 and b"Acme" in seen[0].content and b"tisax" in seen[0].content


def test_slack_failure_rolls_back_alert(db, monkeypatch):
    monkeypatch.setattr(route, "make_client", lambda: httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(500))))
    cid = add_company(db)
    enqueue(db, "route", {"company_id": cid})
    db.commit()
    settings = replace(get_settings(), slack_webhook_url="https://hooks.slack.test/x")
    drain(get_pool(), settings)
    company = db.execute("SELECT owner, alerted_at FROM companies").fetchone()
    assert company == {"owner": "anna", "alerted_at": None}  # routed; alert retryable
    jobs = {r["kind"]: r["status"] for r in db.execute("SELECT kind, status FROM jobs")}
    assert jobs == {"route": "done", "alert": "dead", "crm_sync": "done",
                    "crm_contacts": "done"}


def test_alert_text_says_why_now():
    from app.route import alert_text

    company = {"canonical_name": "Müller", "country": "DE", "owner": "anna",
               "why_now": "ISMS Manager (TISAX)", "id": 7}
    text = alert_text(company, {"total": 80, "breakdown": {"tisax": 30}})
    assert "Why now: hiring ISMS Manager (TISAX)" in text and "tisax +30" in text
    assert "Why now" not in alert_text(company | {"why_now": None},
                                       {"total": 80, "breakdown": {}})
