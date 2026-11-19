from psycopg.types.json import Jsonb

from app.profile import company_profile


def seed(db):
    cid = db.execute("INSERT INTO companies (canonical_name, country, employees, industry, owner) "
                     "VALUES ('Müller Automotive GmbH', 'DE', 120, 'automotive', 'anna') "
                     "RETURNING id").fetchone()["id"]
    db.execute("INSERT INTO scores (company_id, total, tier, breakdown, model_version) "
               "VALUES (%s, 75, 'A', '{\"tisax\": 30, \"eu_hq\": 15}', 'rules-v1')", (cid,))
    db.execute("INSERT INTO contacts (company_id, email, name, title, seniority) "
               "VALUES (%s, 'jan@mueller.de', 'Jan Secret', 'CISO', 'c_level')", (cid,))
    db.execute("INSERT INTO raw_leads (source, idempotency_key, payload, contact_id) VALUES "
               "('adzuna', 'adzuna:DE:1', %s, 1)",
               (Jsonb({"signal": {"title": "ISMS Manager (TISAX)", "url": "https://adzuna.de/1",
                                  "posted_at": "2026-11-10T08:00:00Z", "query": "TISAX"}}),))
    return cid


def test_profile_has_score_evidence_and_no_personal_data(db):
    p = company_profile(db, seed(db))
    assert p["name"] == "Müller Automotive GmbH" and p["score"]["tier"] == "A"
    assert p["score"]["breakdown"] == {"tisax": 30, "eu_hq": 15}
    assert p["evidence"] == [{"title": "ISMS Manager (TISAX)", "url": "https://adzuna.de/1",
                              "posted_at": "2026-11-10T08:00:00Z", "query": "TISAX"}]
    assert p["contacts"] == [{"title": "CISO", "seniority": "c_level"}]
    assert "jan@mueller.de" not in str(p) and "Jan Secret" not in str(p)
    assert p["crm"] is None and p["brief"] is None


def test_profile_shows_only_approved_brief(db):
    cid = seed(db)
    db.execute("INSERT INTO briefs (company_id, brief, email_draft, status) "
               "VALUES (%s, 'draft text', 'e', 'draft')", (cid,))
    assert company_profile(db, cid)["brief"] is None
    db.execute("UPDATE briefs SET status = 'approved'")
    assert company_profile(db, cid)["brief"]["brief"] == "draft text"


def test_profile_unknown_is_none(db):
    assert company_profile(db, 999) is None


def test_profile_routes(client, db):
    cid = seed(db)
    db.commit()
    r = client.get(f"/api/companies/{cid}")
    assert r.status_code == 200 and r.json()["score"]["total"] == 75
    page = client.get(f"/companies/{cid}")
    assert page.status_code == 200 and "Müller Automotive GmbH" in page.text
    assert 'href="https://adzuna.de/1"' in page.text and "tisax" in page.text
    assert client.get("/api/companies/999").status_code == 404
    assert client.get("/companies/999").status_code == 404


def test_profile_page_escapes(client, db):
    cid = db.execute("INSERT INTO companies (canonical_name) VALUES ('<img src=x onerror=1>') "
                     "RETURNING id").fetchone()["id"]
    db.commit()
    page = client.get(f"/companies/{cid}").text
    assert "<img src=x" not in page and "&lt;img" in page
