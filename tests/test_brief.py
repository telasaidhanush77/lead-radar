from app.brief import make_brief

ADMIN = {"Authorization": "Bearer test-admin"}
COMPANY = {"canonical_name": "Müller Automotive GmbH", "country": "DE", "employees": 250,
           "industry": "automotive", "owner": "anna"}
SCORE = {"total": 90, "tier": "A",
         "breakdown": {"eu_hq": 15, "tisax": 30, "hiring_security": 20, "senior_contact": 10}}
CONTACTS = [{"name": "Jan Weber", "title": "CISO", "seniority": "c_level", "email": "jan@m.de"},
            {"name": "Eva Roth", "title": "Developer", "seniority": "ic", "email": "eva@m.de"}]


def test_template_brief_mentions_tisax_for_supplier():
    brief, email = make_brief(COMPANY, SCORE, CONTACTS, generate=None)
    assert "TISAX" in brief and "ISO 27001" in brief and "Müller Automotive GmbH" in brief
    assert email.startswith("Subject: ") and "Hi Jan" in email and "TISAX" in email


def test_template_without_contacts_still_works():
    brief, email = make_brief(COMPANY, SCORE, [], generate=None)
    assert "Hi there" in email


def test_llm_reply_used_when_it_has_both_sections():
    reply = "BRIEF:\nThey need TISAX.\nEMAIL:\nSubject: Hello\nHi Jan, ..."
    brief, email = make_brief(COMPANY, SCORE, CONTACTS, generate=lambda prompt: reply)
    assert brief == "They need TISAX." and email == "Subject: Hello\nHi Jan, ..."


def test_llm_reply_missing_section_falls_back():
    brief, _ = make_brief(COMPANY, SCORE, CONTACTS, generate=lambda prompt: "just chatting")
    assert "TISAX" in brief and "tier A" in brief


def test_llm_error_falls_back():
    def boom(prompt):
        raise RuntimeError("429")

    assert "TISAX" in make_brief(COMPANY, SCORE, CONTACTS, generate=boom)[0]


def seed(db):
    db.execute("INSERT INTO companies (canonical_name, country, industry, owner) "
               "VALUES ('Müller Automotive GmbH', 'DE', 'automotive', 'anna')")
    db.execute("INSERT INTO scores (company_id, total, tier, breakdown, model_version) "
               "VALUES (1, 90, 'A', '{\"tisax\": 30}', 'rules-v1')")
    db.execute("INSERT INTO contacts (email, name, title, seniority, company_id) "
               "VALUES ('jan@m.de', 'Jan Weber', 'CISO', 'c_level', 1)")
    db.commit()


def test_brief_saved_as_draft(client, db):
    seed(db)
    assert client.post("/companies/1/brief").status_code == 401
    r = client.post("/companies/1/brief", headers=ADMIN)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "draft" and "TISAX" in body["brief"] and body["id"] == 1


def test_brief_for_unscored_company_404(client, db):
    assert client.post("/companies/99/brief", headers=ADMIN).status_code == 404


def test_approve_sets_status_and_is_idempotent(client, db):
    seed(db)
    client.post("/companies/1/brief", headers=ADMIN)
    first = client.post("/briefs/1/approve", headers=ADMIN)
    second = client.post("/briefs/1/approve", headers=ADMIN)
    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == "approved"
    assert first.json()["approved_at"] == second.json()["approved_at"]
    assert client.post("/briefs/9/approve", headers=ADMIN).status_code == 404


def test_agent_steps_saved_and_returned(client, db, monkeypatch):
    from app import brief as brief_module

    seed(db)
    db.execute("INSERT INTO company_domains VALUES ('m.de', 1)")
    db.commit()

    def fake_research(company, score, contacts, domains, llm, fetch):
        assert domains == ["m.de"]
        return "Agent brief", "Subject: Hi\n\nHi Jan", [
            {"tool": "fetch_page", "input": '{"url": "https://m.de/"}', "result": "Brakes",
             "ok": True},
            {"tool": "finish", "input": "", "result": "", "ok": True}]

    monkeypatch.setattr(brief_module, "research", fake_research)
    monkeypatch.setattr(brief_module, "make_agent_llm", lambda: object())
    body = client.post("/companies/1/brief", headers=ADMIN).json()
    assert body["brief"] == "Agent brief" and body["mode"] == "agent"
    assert [s["tool"] for s in body["steps"]] == ["fetch_page", "finish"]
    again = client.get("/briefs/1", headers=ADMIN).json()
    assert again["steps"] == body["steps"]


def test_offline_brief_is_template_mode(client, db):
    seed(db)
    body = client.post("/companies/1/brief", headers=ADMIN).json()
    assert body["mode"] == "template" and body["steps"] == []
    assert client.get("/briefs/1").status_code == 401
