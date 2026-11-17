from fastapi.testclient import TestClient

from app.db import migrate

EXPECTED = {
    "schema_migrations", "raw_leads", "companies", "company_domains", "contacts", "scores",
    "jobs", "crm_sync", "fake_crm", "classify_cache", "briefs",
}


def test_migrate_creates_all_tables(db):
    rows = db.execute("SELECT table_name FROM information_schema.tables "
                      "WHERE table_schema='public'").fetchall()
    assert EXPECTED <= {r["table_name"] for r in rows}


def test_migrate_is_idempotent(db):
    assert migrate(db) == []


def test_healthz_ok(db):
    from app.main import app

    with TestClient(app) as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
