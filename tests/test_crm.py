import json
from dataclasses import replace

import httpx
import pytest

from app import crm as crm_module
from app.config import get_settings
from app.crm import (
    FakeCRM,
    HubSpotCRM,
    company_props,
    crm_contacts,
    crm_sync,
    get_crm,
    props_hash,
)


def add_company(db, name="Acme GmbH", domain="acme.de"):
    cid = db.execute("INSERT INTO companies (canonical_name, country, employees, industry) "
                     "VALUES (%s, 'DE', 120, 'saas') RETURNING id", (name,)).fetchone()["id"]
    if domain:
        db.execute("INSERT INTO company_domains VALUES (%s, %s)", (domain, cid))
    db.execute("INSERT INTO scores (company_id, total, tier, breakdown, model_version) "
               "VALUES (%s, 75, 'A', '{\"tisax\": 30, \"eu_hq\": 15}', 'rules-v1')", (cid,))
    db.execute("INSERT INTO contacts (email, name, title, company_id) VALUES "
               "('jan@acme.de', 'Jan Weber', 'CISO', %s)", (cid,))
    return cid


def sync(db, cid):
    """Both CRM jobs, as the worker would run them."""
    crm_sync(db, {"company_id": cid}, get_settings())
    crm_contacts(db, {"company_id": cid}, get_settings())


def fake_rows(db):
    return db.execute("SELECT object, key, external_id FROM fake_crm ORDER BY object, key"
                      ).fetchall()


def test_company_props_are_standard_hubspot_fields():
    props = company_props({"canonical_name": "Acme", "country": "DE", "employees": 120,
                           "industry": "saas", "owner": "anna"}, "acme.de",
                          {"total": 75, "tier": "A", "breakdown": {"tisax": 30}})
    assert set(props) == {"name", "domain", "country", "numberofemployees", "description"}
    assert "tier A" in props["description"] and "tisax +30" in props["description"]


def test_props_hash_is_order_independent():
    assert props_hash({"a": 1, "b": 2}) == props_hash({"b": 2, "a": 1})


def test_fake_sync_creates_records(db):
    cid = add_company(db)
    sync(db, cid)
    assert fake_rows(db) == [
        {"object": "company", "key": "acme.de", "external_id": "fake-company-acme.de"},
        {"object": "contact", "key": "jan@acme.de", "external_id": "fake-contact-jan@acme.de"}]
    assert db.execute("SELECT external_id FROM crm_sync").fetchone() == {
        "external_id": "fake-company-acme.de"}


def test_resync_unchanged_skips(db, monkeypatch):
    cid = add_company(db)
    sync(db, cid)
    calls = []
    monkeypatch.setattr(FakeCRM, "upsert_company", lambda *a: calls.append(a))
    sync(db, cid)
    assert calls == []


def test_changed_props_resync(db):
    cid = add_company(db)
    sync(db, cid)
    db.execute("UPDATE companies SET employees = 300")
    sync(db, cid)
    props = db.execute("SELECT props FROM fake_crm WHERE object='company'").fetchone()["props"]
    assert props["numberofemployees"] == 300 and len(fake_rows(db)) == 2


def test_new_contact_triggers_resync(db):
    cid = add_company(db)
    sync(db, cid)
    db.execute("INSERT INTO contacts (email, company_id) VALUES ('new@acme.de', %s)", (cid,))
    sync(db, cid)
    assert [r["key"] for r in fake_rows(db)] == ["acme.de", "jan@acme.de", "new@acme.de"]


def test_company_without_domain_keeps_its_crm_record(db):
    cid = add_company(db, domain=None)
    sync(db, cid)
    db.execute("UPDATE companies SET employees = 300")
    sync(db, cid)
    assert len([r for r in fake_rows(db) if r["object"] == "company"]) == 1


def hubspot(responses: dict, seen: list) -> HubSpotCRM:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json=responses.get((request.method, request.url.path), {}))

    return HubSpotCRM("tok", httpx.Client(transport=httpx.MockTransport(handler),
                                          base_url="https://api.hubapi.com"))


def test_hubspot_search_then_patch():
    seen = []
    crm = hubspot({("POST", "/crm/v3/objects/companies/search"):
                   {"total": 1, "results": [{"id": "77"}]}}, seen)
    assert crm.upsert_company("acme.de", {"name": "Acme"}, None) == "77"
    assert seen == [("POST", "/crm/v3/objects/companies/search"),
                    ("PATCH", "/crm/v3/objects/companies/77")]


def test_hubspot_create_when_missing():
    seen = []
    crm = hubspot({("POST", "/crm/v3/objects/companies/search"): {"total": 0, "results": []},
                   ("POST", "/crm/v3/objects/companies"): {"id": "88"}}, seen)
    assert crm.upsert_company("acme.de", {"name": "Acme"}, None) == "88"
    assert seen[-1] == ("POST", "/crm/v3/objects/companies")


def test_hubspot_known_id_patches_directly():
    seen = []
    assert hubspot({}, seen).upsert_company(None, {"name": "Acme"}, "55") == "55"
    assert seen == [("PATCH", "/crm/v3/objects/companies/55")]


def test_hubspot_contact_upsert_by_email_and_associate():
    seen, bodies = [], []

    def handler(request):
        seen.append((request.method, request.url.path))
        bodies.append(json.loads(request.content or b"{}"))
        return httpx.Response(200, json={"results": [{"id": "9"}]})

    crm = HubSpotCRM("tok", httpx.Client(transport=httpx.MockTransport(handler),
                                         base_url="https://api.hubapi.com"))
    assert crm.upsert_contact("jan@acme.de", {"email": "jan@acme.de"}, "77") == "9"
    assert seen == [("POST", "/crm/v3/objects/contacts/batch/upsert"),
                    ("PUT", "/crm/v4/objects/contacts/9/associations/default/companies/77")]
    assert bodies[0]["inputs"][0] == {"idProperty": "email", "id": "jan@acme.de",
                                      "properties": {"email": "jan@acme.de"}}


def test_hubspot_without_token_fails_fast(db):
    with pytest.raises(ValueError, match="HUBSPOT_TOKEN"):
        get_crm(replace(get_settings(), crm_provider="hubspot", hubspot_token=""), db)


def test_concurrent_syncs_create_one_crm_company(db):
    import threading

    import psycopg
    from psycopg.rows import dict_row

    from tests.conftest import TEST_DB

    cid = add_company(db, domain=None)
    db.commit()
    first = psycopg.connect(TEST_DB, row_factory=dict_row)
    crm_sync(first, {"company_id": cid}, get_settings())  # not committed yet: holds the lock
    done = []

    def second():
        with psycopg.connect(TEST_DB, row_factory=dict_row) as conn:
            crm_sync(conn, {"company_id": cid}, get_settings())
            done.append(True)

    t = threading.Thread(target=second)
    t.start()
    t.join(0.5)
    assert done == []  # waits for the first sync instead of racing it
    first.commit()
    first.close()
    t.join(5)
    companies = db.execute("SELECT count(*) AS n FROM fake_crm WHERE object = 'company'")
    assert companies.fetchone()["n"] == 1


class FlakyCRM:
    def __init__(self):
        self.creates = 0

    def upsert_company(self, domain, props, external_id):
        if external_id:
            return external_id
        self.creates += 1
        return f"hs-{self.creates}"

    def upsert_contact(self, email, props, company_external_id):
        raise RuntimeError("HTTP 400: invalid email")


def test_contact_failure_never_recreates_company(db, monkeypatch):
    from app import queue
    from app.db import get_pool
    from app.worker import drain

    flaky = FlakyCRM()
    monkeypatch.setattr(crm_module, "get_crm", lambda settings, conn: flaky)
    cid = add_company(db, domain=None)
    queue.enqueue(db, "crm_sync", {"company_id": cid})
    db.commit()
    drain(get_pool(), get_settings())
    assert flaky.creates == 1
    statuses = {r["kind"]: r["status"] for r in db.execute("SELECT kind, status FROM jobs")}
    assert statuses == {"crm_sync": "done", "crm_contacts": "dead"}


def test_hubspot_create_is_not_retried_after_5xx():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"total": 0, "results": []})
        return httpx.Response(503)

    crm = HubSpotCRM("tok", httpx.Client(transport=httpx.MockTransport(handler),
                                         base_url="https://api.hubapi.com"))
    with pytest.raises(Exception, match="503"):
        crm.upsert_company("acme.de", {"name": "Acme"}, None)
    assert seen.count(("POST", "/crm/v3/objects/companies")) == 1


def test_hubspot_company_without_domain_is_found_by_exact_name():
    """Found live: a domainless company was created twice once local sync state was lost."""
    seen, bodies = [], []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/search"):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"total": 1, "results": [{"id": "91"}]})
        return httpx.Response(200, json={})

    crm = HubSpotCRM("tok", httpx.Client(transport=httpx.MockTransport(handler),
                                         base_url="https://api.hubapi.com"))
    assert crm.upsert_company(None, {"name": "Aurora Logistics S.p.A."}, None) == "91"
    assert bodies[0]["filterGroups"][0]["filters"] == [
        {"propertyName": "name", "operator": "EQ", "value": "Aurora Logistics S.p.A."}]
    assert seen[-1] == ("PATCH", "/crm/v3/objects/companies/91")
