"""Who works this company: tier -> queue, region -> owner (sticky), tier A -> one AE alert."""

import logging

import httpx
import psycopg

from app.config import Settings
from app.queue import enqueue

log = logging.getLogger("route")

REGIONS = {
    "DACH": {"DE", "AT", "CH"},
    "Benelux": {"BE", "NL", "LU"},
    "Nordics": {"SE", "NO", "DK", "FI", "IS"},
}
OWNERS = {  # fictional reps, in round-robin order
    "DACH": ["anna", "jonas"],
    "Benelux": ["sem"],
    "Nordics": ["freja"],
    "Rest": ["lucas", "marta"],
}
ROUTES = {"A": "ae", "B": "bdr", "C": "nurture"}


def region_for(country: str | None) -> str:
    return next((name for name, codes in REGIONS.items() if country in codes), "Rest")


def route_for(tier: str) -> str:
    return ROUTES[tier]


def assign_owner(conn: psycopg.Connection, company_id: int, region: str) -> str:
    """Keep the existing owner; otherwise the region's rep with the fewest companies
    (ties -> list order). The WHERE owner IS NULL makes the assignment stick."""
    owner = conn.execute(
        """
        SELECT o.owner
        FROM unnest(%s::text[]) WITH ORDINALITY AS o(owner, pos)
        LEFT JOIN companies c ON c.owner = o.owner
        GROUP BY o.owner, o.pos
        ORDER BY count(c.id), o.pos
        LIMIT 1
        """, (OWNERS[region],)).fetchone()["owner"]
    conn.execute("UPDATE companies SET owner = %s WHERE id = %s AND owner IS NULL",
                 (owner, company_id))
    return conn.execute("SELECT owner FROM companies WHERE id = %s",
                        (company_id,)).fetchone()["owner"]


def claim_alert(conn: psycopg.Connection, company_id: int) -> bool:
    """Compare-and-set: true for exactly one caller, ever (until a rollback undoes it)."""
    return conn.execute("UPDATE companies SET alerted_at = now() "
                        "WHERE id = %s AND alerted_at IS NULL RETURNING id",
                        (company_id,)).fetchone() is not None


def make_client() -> httpx.Client:
    return httpx.Client(timeout=10)


def send_alert(settings: Settings, company: dict, score: dict) -> None:
    reasons = ", ".join(f"{rule} +{pts}" for rule, pts in score["breakdown"].items())
    text = (f":rotating_light: Tier A lead: *{company['canonical_name']}* "
            f"({company['country'] or '?'}) score {score['total']}, owner {company['owner']}. "
            f"Why: {reasons}")
    if not settings.slack_webhook_url:
        log.info("ae_alert", extra={"company_id": company["id"], "text": text})
        return
    with make_client() as client:
        client.post(settings.slack_webhook_url, json={"text": text}).raise_for_status()


def route_company(conn: psycopg.Connection, payload: dict, settings: Settings) -> None:
    """Job handler. If the Slack call raises, the whole transaction (alerted_at included)
    rolls back and the job retries, so the alert is neither lost nor sent twice on success."""
    company_id = payload["company_id"]
    score = conn.execute("SELECT total, tier, breakdown FROM scores WHERE company_id = %s",
                         (company_id,)).fetchone()
    country = conn.execute("SELECT country FROM companies WHERE id = %s",
                           (company_id,)).fetchone()["country"]
    assign_owner(conn, company_id, region_for(country))
    conn.execute("UPDATE companies SET route = %s, updated_at = now() WHERE id = %s",
                 (route_for(score["tier"]), company_id))
    if score["tier"] == "A" and claim_alert(conn, company_id):
        company = conn.execute("SELECT id, canonical_name, country, owner FROM companies "
                               "WHERE id = %s", (company_id,)).fetchone()
        send_alert(settings, company, score)
    enqueue(conn, "crm_sync", {"company_id": company_id})
