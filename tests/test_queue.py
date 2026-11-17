import threading

import psycopg
from psycopg.rows import dict_row

from app import queue
from tests.conftest import TEST_DB


def test_enqueue_then_claim(db):
    job_id = queue.enqueue(db, "process_lead", {"raw_lead_id": 7})
    db.commit()
    job = queue.claim(db)
    assert job.id == job_id and job.kind == "process_lead"
    assert job.payload == {"raw_lead_id": 7} and job.attempts == 1
    assert queue.claim(db) is None  # it is now running, nobody else gets it


def test_claim_respects_run_after(db):
    queue.enqueue(db, "x", {}, delay_s=60)
    db.commit()
    assert queue.claim(db) is None


def test_fail_backs_off_then_dead(db):
    queue.enqueue(db, "x", {})
    statuses = []
    for _ in range(3):
        job = queue.claim(db)
        statuses.append(queue.fail(db, job, "boom", backoff_base_s=0))
    assert statuses == ["queued", "queued", "dead"]
    row = db.execute("SELECT status, attempts, last_error FROM jobs").fetchone()
    assert row == {"status": "dead", "attempts": 3, "last_error": "boom"}


def test_fail_sets_exponential_run_after(db):
    queue.enqueue(db, "x", {})
    job = queue.claim(db)
    queue.fail(db, job, "boom", backoff_base_s=10)  # attempt 1 -> 10 * 2**1 = 20 s
    delay = db.execute("SELECT extract(epoch FROM run_after - now()) AS s FROM jobs").fetchone()
    assert 18 < delay["s"] <= 20


def test_last_error_truncated(db):
    queue.enqueue(db, "x", {})
    queue.fail(db, queue.claim(db), "e" * 5000, backoff_base_s=0)
    assert len(db.execute("SELECT last_error FROM jobs").fetchone()["last_error"]) == 2000


def test_retry_only_dead(db):
    job_id = queue.enqueue(db, "x", {})
    assert queue.retry(db, job_id) is False  # queued, not dead
    db.execute("UPDATE jobs SET status='dead', attempts=3")
    assert queue.retry(db, job_id) is True
    assert db.execute("SELECT status, attempts FROM jobs").fetchone() == {
        "status": "queued", "attempts": 0}


def test_complete_marks_done(db):
    queue.enqueue(db, "x", {})
    queue.complete(db, queue.claim(db).id)
    assert queue.counts(db) == {"done": 1}


def test_stale_running_job_is_reclaimed(db):
    queue.enqueue(db, "x", {})
    queue.claim(db)
    assert queue.claim(db) is None
    db.execute("UPDATE jobs SET updated_at = now() - interval '6 minutes'")
    job = queue.claim(db)
    assert job is not None and job.attempts == 2


def test_concurrent_claim_never_duplicates(db):
    for i in range(50):
        queue.enqueue(db, "x", {"i": i})
    db.commit()
    seen: list[list[int]] = [[], []]

    def work(slot: int) -> None:
        with psycopg.connect(TEST_DB, row_factory=dict_row) as conn:
            while (job := queue.claim(conn)) is not None:
                conn.commit()  # claim is visible to the other worker before we finish
                seen[slot].append(job.id)
                queue.complete(conn, job.id)
                conn.commit()

    threads = [threading.Thread(target=work, args=(s,)) for s in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(seen[0]) + len(seen[1]) == 50
    assert set(seen[0]).isdisjoint(seen[1])
