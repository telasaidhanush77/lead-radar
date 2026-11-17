"""How badly does this company need compliance automation? Transparent points, no black box."""

from collections.abc import Iterable

import psycopg
from psycopg.types.json import Jsonb

from app.config import Settings
from app.queue import enqueue

MODEL_VERSION = "rules-v1"
WEIGHTS = {
    "eu_hq": 15,            # GDPR applies; European buyers ask for ISO 27001
    "size_fit": 15,         # 20-500 staff: big enough to be asked, too small for a GRC team
    "tisax": 30,            # automotive supplier: OEMs require TISAX, a hard deadline
    "soc2": 20,             # US enterprise customers ask for SOC 2 in procurement
    "hiring_security": 20,  # they are building security right now
    "regulated": 15,        # finance / health / insurance / critical infrastructure
    "senior_contact": 10,   # the lead can actually buy
}
TIER_A, TIER_B = 70, 40
SIZE_MIN, SIZE_MAX = 20, 500
EUROPE = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT",
    "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",  # EU27
    "GB", "CH", "NO", "IS", "LI",
})
SENIOR = frozenset({"c_level", "vp", "director"})
SENIORITY_RANK = {"c_level": 5, "vp": 4, "director": 3, "manager": 2, "ic": 1, "unknown": 0}


def tier_for(total: int) -> str:
    return "A" if total >= TIER_A else "B" if total >= TIER_B else "C"


def score(country: str | None, employees: int | None, signals: Iterable[str],
          best_seniority: str) -> tuple[int, str, dict[str, int]]:
    """(total, tier, breakdown). The breakdown holds only rules that fired, so a rep sees why."""
    signals = set(signals)
    fired = {
        "eu_hq": country in EUROPE,
        "size_fit": employees is not None and SIZE_MIN <= employees <= SIZE_MAX,
        "tisax": "automotive_supplier" in signals,
        "soc2": "us_enterprise_customers" in signals,
        "hiring_security": "hiring_security" in signals,
        "regulated": "regulated_industry" in signals,
        "senior_contact": best_seniority in SENIOR,
    }
    breakdown = {rule: WEIGHTS[rule] for rule, hit in fired.items() if hit}
    total = sum(breakdown.values())
    return total, tier_for(total), breakdown


def score_company(conn: psycopg.Connection, payload: dict, settings: Settings) -> None:
    """Job handler: score from the company + its most senior contact, upsert, queue routing."""
    company_id = payload["company_id"]
    company = conn.execute("SELECT country, employees, signals FROM companies WHERE id = %s",
                           (company_id,)).fetchone()
    seniorities = [r["seniority"] for r in conn.execute(
        "SELECT seniority FROM contacts WHERE company_id = %s", (company_id,))]
    best = max(seniorities, key=lambda s: SENIORITY_RANK.get(s, 0), default="unknown")
    total, tier, breakdown = score(company["country"], company["employees"], company["signals"],
                                   best)
    conn.execute(
        """
        INSERT INTO scores (company_id, total, tier, breakdown, model_version)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (company_id) DO UPDATE SET total = EXCLUDED.total, tier = EXCLUDED.tier,
            breakdown = EXCLUDED.breakdown, model_version = EXCLUDED.model_version,
            scored_at = now()
        """, (company_id, total, tier, Jsonb(breakdown), MODEL_VERSION))
    enqueue(conn, "route", {"company_id": company_id})
