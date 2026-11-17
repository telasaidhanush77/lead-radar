from app import queue
from app.config import get_settings
from app.db import get_pool
from app.worker import HANDLERS, drain, run_once


def test_run_once_empty_returns_false(db):
    assert run_once(get_pool(), get_settings()) is False


def test_handler_success_marks_done(db, monkeypatch):
    seen = []
    monkeypatch.setitem(HANDLERS, "ok", lambda conn, payload, settings: seen.append(payload))
    queue.enqueue(db, "ok", {"n": 1})
    db.commit()
    assert drain(get_pool(), get_settings()) == 1
    assert seen == [{"n": 1}] and queue.counts(db) == {"done": 1}


def test_handler_error_retries_then_dead(db, monkeypatch):
    def boom(conn, payload, settings):
        raise RuntimeError("enrich API down")

    monkeypatch.setitem(HANDLERS, "boom", boom)
    queue.enqueue(db, "boom", {})
    db.commit()
    assert drain(get_pool(), get_settings()) == 3
    row = db.execute("SELECT status, attempts, last_error FROM jobs").fetchone()
    assert row == {"status": "dead", "attempts": 3, "last_error": "RuntimeError: enrich API down"}


def test_handler_failure_rolls_back_its_writes(db, monkeypatch):
    def half_done(conn, payload, settings):
        conn.execute("INSERT INTO companies (canonical_name) VALUES ('ghost')")
        raise RuntimeError("crash after writing")

    monkeypatch.setitem(HANDLERS, "half", half_done)
    queue.enqueue(db, "half", {})
    db.commit()
    drain(get_pool(), get_settings())
    assert db.execute("SELECT count(*) AS n FROM companies").fetchone()["n"] == 0


def test_unknown_kind_goes_dead(db):
    queue.enqueue(db, "no_such_kind", {})
    db.commit()
    drain(get_pool(), get_settings())
    assert queue.counts(db) == {"dead": 1}
