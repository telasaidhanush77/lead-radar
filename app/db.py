from functools import lru_cache
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import get_settings

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply migrations/*.sql in name order, each once. Returns the names applied now."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    done = {row[0] if isinstance(row, tuple) else row["name"]
            for row in conn.execute("SELECT name FROM schema_migrations")}
    applied = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name in done:
            continue
        with conn.transaction():
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
        applied.append(path.name)
    conn.commit()
    return applied


@lru_cache
def get_pool() -> ConnectionPool:
    return ConnectionPool(
        get_settings().database_url, kwargs={"row_factory": dict_row}, min_size=1, max_size=5,
        open=True,
    )
