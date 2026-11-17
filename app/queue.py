"""A job queue in one Postgres table. Callers own the transaction; nothing here commits."""

from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

STALE_AFTER = "5 minutes"  # a 'running' job untouched this long belongs to a dead worker
MAX_ERROR_CHARS = 2000


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    payload: dict
    attempts: int
    max_attempts: int


def enqueue(conn: psycopg.Connection, kind: str, payload: dict, delay_s: float = 0) -> int:
    row = conn.execute(
        "INSERT INTO jobs (kind, payload, run_after) "
        "VALUES (%s, %s, now() + make_interval(secs => %s)) RETURNING id",
        (kind, Jsonb(payload), delay_s),
    ).fetchone()
    return row["id"]


def claim(conn: psycopg.Connection) -> Job | None:
    """Take the next ready job. SKIP LOCKED lets many workers claim in parallel without
    blocking on, or double-taking, a row another worker is claiming right now."""
    row = conn.execute(
        f"""
        UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = now()
        WHERE id = (
            SELECT id FROM jobs
            WHERE (status = 'queued' AND run_after <= now())
               OR (status = 'running' AND updated_at < now() - interval '{STALE_AFTER}')
            ORDER BY run_after, id
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, kind, payload, attempts, max_attempts
        """
    ).fetchone()
    return Job(**row) if row else None


def complete(conn: psycopg.Connection, job_id: int) -> None:
    conn.execute("UPDATE jobs SET status = 'done', updated_at = now() WHERE id = %s", (job_id,))


def fail(conn: psycopg.Connection, job: Job, error: str, backoff_base_s: float) -> str:
    """Requeue with exponential backoff (base * 2**attempts), or 'dead' when out of attempts."""
    status = "dead" if job.attempts >= job.max_attempts else "queued"
    conn.execute(
        "UPDATE jobs SET status = %s, last_error = %s, updated_at = now(), "
        "run_after = now() + make_interval(secs => %s) WHERE id = %s",
        (status, error[:MAX_ERROR_CHARS], backoff_base_s * 2**job.attempts, job.id),
    )
    return status


def retry(conn: psycopg.Connection, job_id: int) -> bool:
    """Give a dead job a fresh set of attempts. Only dead jobs; returns whether one changed."""
    cur = conn.execute(
        "UPDATE jobs SET status = 'queued', attempts = 0, run_after = now(), updated_at = now() "
        "WHERE id = %s AND status = 'dead'",
        (job_id,),
    )
    return cur.rowcount == 1


def counts(conn: psycopg.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, count(*) AS n FROM jobs GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}
