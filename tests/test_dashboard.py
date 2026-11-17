from app import queue
from app.dashboard import render_dashboard

ADMIN = {"Authorization": "Bearer test-admin"}


def seed_company(db, name, tier, total):
    cid = db.execute("INSERT INTO companies (canonical_name, country, owner) "
                     "VALUES (%s, 'DE', 'anna') RETURNING id", (name,)).fetchone()["id"]
    db.execute("INSERT INTO scores (company_id, total, tier, breakdown, model_version) "
               "VALUES (%s, %s, %s, '{\"tisax\": 30}', 'rules-v1')", (cid, total, tier))
    return cid


def test_dashboard_renders_counts(db):
    seed_company(db, "Müller Automotive", "A", 90)
    seed_company(db, "Beta Logistics", "B", 45)
    html = render_dashboard(db)
    assert "Müller Automotive" in html and "tisax" in html
    assert "Beta Logistics" not in html  # only tier A is listed
    assert 'data-count="companies">2<' in html


def test_dashboard_escapes_html(db):
    seed_company(db, "<script>alert(1)</script>", "A", 99)
    html = render_dashboard(db)
    assert "<script>alert(1)" not in html and "&lt;script&gt;alert(1)" in html


def test_dashboard_lists_dead_jobs(db):
    job_id = queue.enqueue(db, "crm_sync", {"company_id": 1})
    db.execute("UPDATE jobs SET status='dead', last_error='HTTP 429: slow down'")
    html = render_dashboard(db)
    assert f'data-retry="{job_id}"' in html and "HTTP 429" in html


def test_dashboard_route(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")


def test_retry_requires_token(client):
    assert client.post("/jobs/1/retry").status_code == 401


def test_retry_dead_job(client, db):
    job_id = queue.enqueue(db, "x", {})
    db.execute("UPDATE jobs SET status='dead'")
    db.commit()
    r = client.post(f"/jobs/{job_id}/retry", headers=ADMIN)
    assert r.status_code == 200 and r.json() == {"retried": True}
    assert queue.counts(db) == {"queued": 1}


def test_retry_unknown_404(client):
    assert client.post("/jobs/999/retry", headers=ADMIN).status_code == 404


def test_demo_run_stores_leads_without_ground_truth(client, db):
    assert client.post("/demo/run").status_code == 401
    r = client.post("/demo/run", headers=ADMIN)
    assert r.status_code == 200 and r.json()["accepted"] > 50
    keys = db.execute("SELECT DISTINCT jsonb_object_keys(payload) AS k FROM raw_leads").fetchall()
    assert not any(row["k"].startswith("_") for row in keys)
