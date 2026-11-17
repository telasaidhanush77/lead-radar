"""Runs jobs. Each job's work and its 'done' mark commit in one transaction."""

import logging
import threading
from collections.abc import Callable

import psycopg
from psycopg_pool import ConnectionPool

from app import queue
from app.classify import enrich
from app.config import Settings, get_settings
from app.crm import crm_sync
from app.db import get_pool
from app.dedupe import process_lead
from app.log import job_id
from app.route import route_company
from app.score import score_company

log = logging.getLogger("worker")

Handler = Callable[[psycopg.Connection, dict, Settings], None]
HANDLERS: dict[str, Handler] = {
    "process_lead": process_lead,
    "enrich": enrich,
    "score": score_company,
    "route": route_company,
    "crm_sync": crm_sync,
}
IDLE_SLEEP_S = 0.5


def run_once(pool: ConnectionPool, settings: Settings) -> bool:
    """Claim and run one job. False when nothing was ready."""
    with pool.connection() as conn:  # transaction 1: claim, committed immediately
        job = queue.claim(conn)
    if job is None:
        return False
    token = job_id.set(job.id)
    try:
        try:
            with pool.connection() as conn:  # transaction 2: the work + complete, atomically
                HANDLERS[job.kind](conn, job.payload, settings)
                queue.complete(conn, job.id)
            log.info("job done", extra={"kind": job.kind})
        except Exception as exc:  # transaction 2 rolled back; record the failure separately
            with pool.connection() as conn:
                status = queue.fail(conn, job, f"{type(exc).__name__}: {exc}",
                                    settings.backoff_base_s)
            log.warning("job failed", exc_info=True,
                        extra={"kind": job.kind, "attempt": job.attempts, "now": status})
    finally:
        job_id.reset(token)
    return True


def drain(pool: ConnectionPool, settings: Settings) -> int:
    """Run jobs until none are ready. Returns how many ran. Used by tests and the benchmark."""
    ran = 0
    while run_once(pool, settings):
        ran += 1
    return ran


def run_forever(pool: ConnectionPool, settings: Settings, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            if not run_once(pool, settings):
                stop.wait(IDLE_SLEEP_S)
        except Exception:  # DB blip: keep the worker alive
            log.exception("worker loop error")
            stop.wait(5)


def start_background(settings: Settings) -> threading.Event:
    stop = threading.Event()
    threading.Thread(target=run_forever, args=(get_pool(), settings, stop), daemon=True,
                     name="worker").start()
    return stop


if __name__ == "__main__":  # python -m app.worker
    from app.log import setup_logging

    setup_logging()
    run_forever(get_pool(), get_settings(), threading.Event())
