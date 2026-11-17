"""Entity resolution: decide which company a lead belongs to, and merge what it tells us."""

import psycopg
from psycopg.types.json import Jsonb
from rapidfuzz import fuzz

from app.config import Settings
from app.normalize import NormalizedLead, name_key, normalize_lead
from app.queue import enqueue

MERGED_FIELDS = ("canonical_name", "country", "employees", "description")
DEDUPE_LOCK = 7001  # advisory lock id: one resolver at a time


# ---- pure ---------------------------------------------------------------------------------
def best_match(key: str, candidates: list[tuple[int, str]], threshold: int) -> int | None:
    """Id of the most similar candidate name scoring >= threshold (0-100), ties -> lowest id.
    token_sort_ratio ignores word order: 'automotive mueller' == 'mueller automotive'."""
    best: tuple[float, int] | None = None
    for cid, cand in candidates:
        score = fuzz.token_sort_ratio(key, cand)
        if score >= threshold and (best is None or (-score, cid) < (-best[0], best[1])):
            best = (score, cid)
    return best[1] if best else None


def merge_fields(current: dict, provenance: dict, incoming: dict, lead_id: int,
                 at: str) -> tuple[dict, dict]:
    """Newest non-null value wins, per field. 'Newest' = the lead's received_at, not processing
    order, so a retried old lead can't overwrite fresher data. Returns new dicts."""
    fields, prov = dict(current), dict(provenance)
    for name, value in incoming.items():
        if value is None:
            continue
        seen = prov.get(name)
        if seen is None or at >= seen["at"]:
            fields[name] = value
            prov[name] = {"lead": lead_id, "at": at}
    return fields, prov


# ---- database -----------------------------------------------------------------------------
def resolve_company(conn: psycopg.Connection, lead: NormalizedLead, threshold: int,
                    previous: int | None = None) -> int | None:
    """Domain match, else fuzzy name match in the same country, else the company this raw
    lead resolved to last time (`previous`, keeps reprocessing idempotent), else a new one.
    None when the lead has neither a company domain nor a company name (unassigned)."""
    # ponytail: global lock serializes resolution so two workers can't both create the same
    # company; per-name locks if dedupe throughput ever matters.
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (DEDUPE_LOCK,))
    if lead.domain:
        row = conn.execute("SELECT company_id FROM company_domains WHERE domain = %s",
                           (lead.domain,)).fetchone()
        if row:
            return row["company_id"]
    company_id = None
    if lead.name_key and lead.country:
        # ponytail: scans every company in the country, O(n) per lead; pg_trgm index past ~50k.
        rows = conn.execute(
            "SELECT id, name_key FROM companies WHERE country = %s AND name_key IS NOT NULL",
            (lead.country,),
        ).fetchall()
        company_id = best_match(lead.name_key, [(r["id"], r["name_key"]) for r in rows],
                                threshold)
    if company_id is None:
        company_id = previous
    if company_id is None:
        if not (lead.domain or lead.name_key):
            return None
        company_id = conn.execute(
            "INSERT INTO companies (name_key, country) VALUES (%s, %s) RETURNING id",
            (lead.name_key, lead.country),
        ).fetchone()["id"]
    if lead.domain:
        conn.execute("INSERT INTO company_domains (domain, company_id) VALUES (%s, %s) "
                     "ON CONFLICT (domain) DO NOTHING", (lead.domain, company_id))
    return company_id


def _shouty(name: str | None) -> bool:
    """ALL CAPS or all lowercase: how people type into forms, not how a company spells itself."""
    return bool(name) and (name.isupper() or name.islower())


def _merge_into_company(conn: psycopg.Connection, company_id: int, lead: NormalizedLead,
                        lead_id: int, at: str) -> None:
    row = conn.execute(
        "SELECT canonical_name, country, employees, description, name_key, provenance "
        "FROM companies WHERE id = %s FOR UPDATE", (company_id,)).fetchone()
    display_name = lead.company_name
    if _shouty(display_name) and row["canonical_name"] and not _shouty(row["canonical_name"]):
        display_name = None  # keep the nicely cased name we already have
    incoming = {"canonical_name": display_name, "country": lead.country,
                "employees": lead.employees, "description": lead.description}
    fields, prov = merge_fields({k: row[k] for k in MERGED_FIELDS}, row["provenance"],
                                incoming, lead_id, at)
    conn.execute(
        "UPDATE companies SET canonical_name = %s, country = %s, employees = %s, "
        "description = %s, name_key = %s, provenance = %s, updated_at = now() WHERE id = %s",
        (fields["canonical_name"], fields["country"], fields["employees"],
         fields["description"], name_key(fields["canonical_name"]) or row["name_key"],
         Jsonb(prov), company_id),
    )


def _save_contact(conn: psycopg.Connection, existing_id: int | None, lead: NormalizedLead,
                  company_id: int | None) -> int:
    values = (lead.email, lead.name, lead.title, lead.seniority, lead.department, company_id)
    if existing_id:  # reprocessing the same raw lead updates the contact it made last time
        conn.execute(
            "UPDATE contacts SET email = COALESCE(%s, email), name = COALESCE(%s, name), "
            "title = COALESCE(%s, title), seniority = %s, department = %s, "
            "company_id = COALESCE(%s, company_id), updated_at = now() WHERE id = %s",
            (*values, existing_id))
        return existing_id
    return conn.execute(
        """
        INSERT INTO contacts (email, name, title, seniority, department, company_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (email) DO UPDATE SET
            name = COALESCE(EXCLUDED.name, contacts.name),
            title = COALESCE(EXCLUDED.title, contacts.title),
            seniority = CASE WHEN EXCLUDED.title IS NULL THEN contacts.seniority
                             ELSE EXCLUDED.seniority END,
            department = CASE WHEN EXCLUDED.title IS NULL THEN contacts.department
                              ELSE EXCLUDED.department END,
            company_id = COALESCE(EXCLUDED.company_id, contacts.company_id),
            updated_at = now()
        RETURNING id
        """, values).fetchone()["id"]


def process_lead(conn: psycopg.Connection, payload: dict, settings: Settings) -> None:
    """Job handler: raw lead -> normalized -> company + contact. Safe to run twice."""
    raw = conn.execute("SELECT id, payload, received_at, contact_id FROM raw_leads WHERE id = %s",
                       (payload["raw_lead_id"],)).fetchone()
    lead = normalize_lead(raw["payload"])
    previous = None
    if raw["contact_id"]:
        previous = conn.execute("SELECT company_id FROM contacts WHERE id = %s",
                                (raw["contact_id"],)).fetchone()["company_id"]
    company_id = resolve_company(conn, lead, settings.fuzzy_threshold, previous)
    if company_id is not None:
        _merge_into_company(conn, company_id, lead, raw["id"], raw["received_at"].isoformat())
    contact_id = _save_contact(conn, raw["contact_id"], lead, company_id)
    conn.execute("UPDATE raw_leads SET contact_id = %s WHERE id = %s", (contact_id, raw["id"]))
    if company_id is not None:
        enqueue(conn, "enrich", {"company_id": company_id})
