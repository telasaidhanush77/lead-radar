import json

from app.main import load_demo_cache


def test_startup_loads_demo_cache(db, tmp_path):
    cache = tmp_path / "classify_cache.jsonl"
    rows = [{"provider": "gemini", "content_hash": "h1",
             "result": {"industry": "saas", "signals": []}, "latency_ms": 900},
            {"provider": "groq", "content_hash": "h1",
             "result": {"industry": "saas", "signals": ["hiring_security"]}, "latency_ms": 300}]
    cache.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert load_demo_cache(db, cache) == 2
    assert load_demo_cache(db, cache) == 0  # idempotent
    assert db.execute("SELECT count(*) AS n FROM classify_cache").fetchone()["n"] == 2


def test_missing_cache_file_is_fine(db, tmp_path):
    assert load_demo_cache(db, tmp_path / "nope.jsonl") == 0


def test_healthz_checks_database(client):
    assert client.get("/healthz").json() == {"status": "ok"}
