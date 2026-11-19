"""Push companies + contacts to the CRM, idempotently. Skips when nothing changed."""

import hashlib
import json
from typing import Protocol
from uuid import uuid4

import httpx
import psycopg
from psycopg.types.json import Jsonb

from app.config import Settings
from app.llm import post_json_with_retry
from app.queue import enqueue

HUBSPOT_URL = "https://api.hubapi.com"


class CRM(Protocol):
    def upsert_company(self, domain: str | None, props: dict, external_id: str | None) -> str:
        """Create or update; returns the CRM's id for the company."""

    def upsert_contact(self, email: str, props: dict, company_external_id: str) -> str: ...


class FakeCRM:
    """Offline stand-in: a table with the same upsert semantics as the real CRM."""

    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def _upsert(self, obj: str, key: str, props: dict) -> str:
        return self.conn.execute(
            "INSERT INTO fake_crm (object, key, external_id, props) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (object, key) DO UPDATE SET props = EXCLUDED.props "
            "RETURNING external_id", (obj, key, f"fake-{obj}-{key}", Jsonb(props))
        ).fetchone()["external_id"]

    def upsert_company(self, domain: str | None, props: dict, external_id: str | None) -> str:
        if external_id:
            row = self.conn.execute(
                "UPDATE fake_crm SET props = %s WHERE object = 'company' AND external_id = %s "
                "RETURNING external_id", (Jsonb(props), external_id)).fetchone()
            if row:
                return row["external_id"]
        return self._upsert("company", domain or f"anon-{uuid4().hex[:12]}", props)

    def upsert_contact(self, email: str, props: dict, company_external_id: str) -> str:
        return self._upsert("contact", email, props | {"company": company_external_id})


class HubSpotCRM:
    """HubSpot CRM v3. Companies: known id -> PATCH; else search by domain (or exact name when
    there is no domain) -> PATCH or POST.
    Contacts: batch upsert keyed on email, then the default contact->company association."""

    def __init__(self, token: str, client: httpx.Client | None = None):
        self.client = client or httpx.Client(base_url=HUBSPOT_URL, timeout=30)
        self.headers = {"Authorization": f"Bearer {token}"}

    def _call(self, method: str, path: str, body: dict | None = None,
              idempotent: bool = True) -> dict:
        return post_json_with_retry(self.client, path, json=body, headers=self.headers,
                                    method=method, idempotent=idempotent)

    def upsert_company(self, domain: str | None, props: dict, external_id: str | None) -> str:
        # Match on domain; a company with no domain falls back to its exact name, or losing
        # our crm_sync row would create it again. ponytail: exact-name match can merge two
        # namesakes; HubSpot's search has no country-scoped fuzzy match to do better.
        key = ("domain", domain) if domain else ("name", props.get("name"))
        if not external_id and key[1]:
            found = self._call("POST", "/crm/v3/objects/companies/search", {
                "filterGroups": [{"filters": [
                    {"propertyName": key[0], "operator": "EQ", "value": key[1]}]}],
                "properties": [key[0]], "limit": 1})
            if found.get("results"):
                external_id = found["results"][0]["id"]
        if external_id:
            self._call("PATCH", f"/crm/v3/objects/companies/{external_id}", {"properties": props})
            return external_id
        # a create is not safe to repeat: after a 5xx it may have happened, so don't retry it
        return self._call("POST", "/crm/v3/objects/companies", {"properties": props},
                          idempotent=False)["id"]

    def upsert_contact(self, email: str, props: dict, company_external_id: str) -> str:
        result = self._call("POST", "/crm/v3/objects/contacts/batch/upsert", {
            "inputs": [{"idProperty": "email", "id": email, "properties": props}]})
        contact_id = result["results"][0]["id"]
        self._call("PUT", f"/crm/v4/objects/contacts/{contact_id}/associations/default/"
                          f"companies/{company_external_id}")
        return contact_id


def get_crm(settings: Settings, conn: psycopg.Connection) -> CRM:
    if settings.crm_provider == "fake":
        return FakeCRM(conn)
    if settings.crm_provider == "hubspot":
        if not settings.hubspot_token:
            raise ValueError("CRM_PROVIDER=hubspot but HUBSPOT_TOKEN is not set")
        return HubSpotCRM(settings.hubspot_token)
    raise ValueError(f"unknown CRM_PROVIDER {settings.crm_provider!r}")


def props_hash(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def company_props(company: dict, domain: str | None, score: dict) -> dict:
    """Standard HubSpot company fields only; our score rides in the description.
    (Custom properties would need creating in the portal first.)"""
    reasons = ", ".join(f"{rule} +{pts}" for rule, pts in score["breakdown"].items())
    return {
        "name": company["canonical_name"],
        "domain": domain,
        "country": company["country"],
        "numberofemployees": company["employees"],
        "description": (f"Lead Radar: tier {score['tier']}, score {score['total']} ({reasons}). "
                        f"Industry: {company['industry']}. Owner: {company['owner']}."),
    }


def contact_props(contact: dict) -> dict:
    first, _, last = (contact["name"] or "").partition(" ")
    return {"email": contact["email"], "firstname": first or None, "lastname": last or None,
            "jobtitle": contact["title"]}


def _sync_inputs(conn: psycopg.Connection, company_id: int) -> tuple:
    """(domain, company props, contact props, digest). Locks the company row first, so two
    sync jobs for one company run one after the other instead of both creating it."""
    company = conn.execute("SELECT canonical_name, country, employees, industry, owner "
                           "FROM companies WHERE id = %s FOR UPDATE", (company_id,)).fetchone()
    domain_row = conn.execute("SELECT domain FROM company_domains WHERE company_id = %s "
                              "ORDER BY domain LIMIT 1", (company_id,)).fetchone()
    domain = domain_row["domain"] if domain_row else None
    score = conn.execute("SELECT total, tier, breakdown FROM scores WHERE company_id = %s",
                         (company_id,)).fetchone()
    props = company_props(company, domain, score)
    contacts = [contact_props(c) for c in conn.execute(
        "SELECT email, name, title FROM contacts WHERE company_id = %s AND email IS NOT NULL "
        "ORDER BY email", (company_id,))]
    return domain, props, contacts, props_hash({"company": props, "contacts": contacts})


def _synced(conn: psycopg.Connection, company_id: int) -> dict | None:
    return conn.execute("SELECT external_id, last_synced_hash FROM crm_sync "
                        "WHERE company_id = %s", (company_id,)).fetchone()


def crm_sync(conn: psycopg.Connection, payload: dict, settings: Settings) -> None:
    """Job handler, step 1: upsert the company and COMMIT its CRM id before touching
    contacts, so a contact failure can never make a retry create the company again.
    The hash covers company AND contacts: unchanged -> no calls at all."""
    company_id = payload["company_id"]
    domain, props, _, digest = _sync_inputs(conn, company_id)
    synced = _synced(conn, company_id)
    if synced and synced["last_synced_hash"] == digest:
        return
    external_id = get_crm(settings, conn).upsert_company(
        domain, props, synced["external_id"] if synced else None)
    conn.execute(
        "INSERT INTO crm_sync (company_id, external_id) VALUES (%s, %s) "
        "ON CONFLICT (company_id) DO UPDATE SET external_id = EXCLUDED.external_id",
        (company_id, external_id))
    enqueue(conn, "crm_contacts", {"company_id": company_id})


def crm_contacts(conn: psycopg.Connection, payload: dict, settings: Settings) -> None:
    """Job handler, step 2: upsert each contact (keyed by email, so retries are safe),
    then record the hash. Contacts without an email can't be keyed: skipped."""
    company_id = payload["company_id"]
    _, _, contacts, digest = _sync_inputs(conn, company_id)
    synced = _synced(conn, company_id)
    if synced["last_synced_hash"] == digest:
        return
    crm = get_crm(settings, conn)
    for contact in contacts:
        crm.upsert_contact(contact["email"], contact, synced["external_id"])
    conn.execute("UPDATE crm_sync SET last_synced_hash = %s, synced_at = now() "
                 "WHERE company_id = %s", (digest, company_id))
