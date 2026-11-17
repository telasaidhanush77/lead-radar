from app import queue
from app.config import get_settings
from app.db import get_pool
from app.ingest import store_lead
from app.worker import drain

LEAD = {
    "email": "jan.weber@mueller-auto.de", "name": "Jan Weber", "title": "CISO",
    "company": "Müller Automotive GmbH", "country": "Germany", "employees": "250",
    "description": "Tier-1 Zulieferer of brake systems for BMW and Audi. "
                   "We are hiring a security engineer.",
}


def test_one_lead_end_to_end(db):
    store_lead(db, "test", "e2e-1", LEAD)
    db.commit()
    assert drain(get_pool(), get_settings()) == 5  # process -> enrich -> score -> route -> crm
    assert queue.counts(db) == {"done": 5}
    company = db.execute("SELECT industry, route, owner, alerted_at IS NOT NULL AS alerted "
                         "FROM companies").fetchone()
    assert company == {"industry": "automotive", "route": "ae", "owner": "anna", "alerted": True}
    score = db.execute("SELECT total, tier FROM scores").fetchone()
    assert score == {"total": 90, "tier": "A"}  # eu 15 + size 15 + tisax 30 + hiring 20 + c 10
    assert db.execute("SELECT count(*) AS n FROM fake_crm").fetchone()["n"] == 2


def test_same_company_twice_one_alert_one_crm_record(db):
    store_lead(db, "test", "e2e-1", LEAD)
    store_lead(db, "test", "e2e-2", LEAD | {"email": "eva@mueller-auto.de", "title": "CTO"})
    db.commit()
    drain(get_pool(), get_settings())
    assert db.execute("SELECT count(*) AS n FROM companies").fetchone()["n"] == 1
    crm = db.execute("SELECT object, count(*) AS n FROM fake_crm GROUP BY object "
                     "ORDER BY object").fetchall()
    assert crm == [{"object": "company", "n": 1}, {"object": "contact", "n": 2}]
