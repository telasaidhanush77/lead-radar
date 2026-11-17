import json
from dataclasses import replace

import httpx
import pytest

from app.config import get_settings
from app.crm import FakeCRM, HubSpotCRM, company_props, crm_sync, get_crm, props_hash


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
    crm_sync(db, {"company_id": cid}, get_settings())
    assert fake_rows(db) == [
        {"object": "company", "key": "acme.de", "external_id": "fake-company-acme.de"},
        {"object": "contact", "key": "jan@acme.de", "external_id": "fake-contact-jan@acme.de"}]
    assert db.execute("SELECT external_id FROM crm_sync").fetchone() == {
        "external_id": "fake-company-acme.de"}


def test_resync_unchanged_skips(db, monkeypatch):
    cid = add_company(db)
    crm_sync(db, {"company_id": cid}, get_settings())
    calls = []
    monkeypatch.setattr(FakeCRM, "upsert_company", lambda *a: calls.append(a))
    crm_sync(db, {"company_id": cid}, get_settings())
    assert calls == []


def test_changed_props_resync(db):
    cid = add_company(db)
    crm_sync(db, {"company_id": cid}, get_settings())
    db.execute("UPDATE companies SET employees = 300")
    crm_sync(db, {"company_id": cid}, get_settings())
    props = db.execute("SELECT props FROM fake_crm WHERE object='company'").fetchone()["props"]
    assert props["numberofemployees"] == 300 and len(fake_rows(db)) == 2


def test_new_contact_triggers_resync(db):
    cid = add_company(db)
    crm_sync(db, {"company_id": cid}, get_settings())
    db.execute("INSERT INTO contacts (email, company_id) VALUES ('new@acme.de', %s)", (cid,))
    crm_sync(db, {"company_id": cid}, get_settings())
    assert [r["key"] for r in fake_rows(db)] == ["acme.de", "jan@acme.de", "new@acme.de"]


def test_company_without_domain_keeps_its_crm_record(db):
    cid = add_company(db, domain=None)
    crm_sync(db, {"company_id": cid}, get_settings())
    db.execute("UPDATE companies SET employees = 300")
    crm_sync(db, {"company_id": cid}, get_settings())
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
