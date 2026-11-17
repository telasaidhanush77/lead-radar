import os

import psycopg
import pytest
from psycopg.rows import dict_row

TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql://lead:lead@localhost:5433/lead_test")
os.environ["DATABASE_URL"] = TEST_DB  # app code under test must never touch a real DB
os.environ["ADMIN_TOKEN"] = "test-admin"
os.environ["WEBHOOK_SECRET"] = "test-secret"
os.environ["BACKOFF_BASE_S"] = "0"
for key in ("LLM_PROVIDER", "CRM_PROVIDER", "RUN_WORKER", "DEMO_MODE", "SLACK_WEBHOOK_URL"):
    os.environ.pop(key, None)


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def _schema():
    from app.db import migrate

    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        migrate(conn)


@pytest.fixture
def db(_schema):
    with psycopg.connect(TEST_DB, row_factory=dict_row) as conn:
        tables = [
            r["tablename"]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                "AND tablename <> 'schema_migrations'"
            )
        ]
        conn.execute(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")
        conn.commit()
        yield conn
        conn.rollback()
