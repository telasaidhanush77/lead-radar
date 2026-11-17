from app.config import get_settings
from app.db import get_pool
from app.dedupe import best_match, merge_fields
from app.ingest import store_lead
from app.worker import drain


# ---- pure ---------------------------------------------------------------------------------
def test_best_match_threshold():
    cands = [(1, "mueller automotive"), (2, "schmidt logistik")]
    assert best_match("mueler automotive", cands, threshold=90) == 1  # one-letter typo
    assert best_match("mueller automotive", [(2, "schmidt logistik")], threshold=90) is None


def test_best_match_word_order_insensitive():
    assert best_match("automotive mueller", [(5, "mueller automotive")], threshold=90) == 5


def test_best_match_tie_lowest_id():
    assert best_match("acme", [(9, "acme"), (3, "acme")], threshold=90) == 3


def test_merge_newer_wins():
    fields, prov = merge_fields({"country": "DE"}, {"country": {"lead": 1, "at": "2026-01-01"}},
                                {"country": "AT"}, lead_id=2, at="2026-02-01")
    assert fields["country"] == "AT" and prov["country"] == {"lead": 2, "at": "2026-02-01"}


def test_merge_older_does_not_overwrite():
    fields, prov = merge_fields({"country": "DE"}, {"country": {"lead": 2, "at": "2026-02-01"}},
                                {"country": "AT"}, lead_id=1, at="2026-01-01")
    assert fields["country"] == "DE" and prov["country"]["lead"] == 2


def test_merge_null_never_overwrites():
    fields, _ = merge_fields({"employees": 200}, {"employees": {"lead": 1, "at": "2026-01-01"}},
                             {"employees": None}, lead_id=2, at="2026-02-01")
    assert fields["employees"] == 200


def test_merge_does_not_mutate():
    current, prov = {"country": "DE"}, {}
    merge_fields(current, prov, {"country": "AT"}, lead_id=1, at="2026-01-01")
    assert current == {"country": "DE"} and prov == {}


# ---- through the worker -------------------------------------------------------------------
def ingest(db, *payloads):
    for i, p in enumerate(payloads):
        store_lead(db, "test", f"k{i}", p)
    db.commit()
    drain(get_pool(), get_settings())


def count(db, table):
    return db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def test_same_domain_same_company(db):
    ingest(db, {"email": "a@acme.de", "company": "Acme GmbH", "country": "DE"},
           {"email": "b@acme.de", "company": "ACME Group"})
    assert count(db, "companies") == 1 and count(db, "contacts") == 2


def test_umlaut_variants_merge(db):
    ingest(db,
           {"email": "jan@mueller-auto.de", "company": "Müller Automotive GmbH", "country": "DE"},
           {"email": "jan.w@gmail.com", "company": "MUELLER Automotive", "country": "Germany"})
    assert count(db, "companies") == 1
    sql = "SELECT count(*) AS n FROM contacts WHERE company_id = 1"
    assert db.execute(sql).fetchone()["n"] == 2


def test_name_match_adds_new_domain(db):
    ingest(db, {"email": "a@acme.de", "company": "Acme GmbH", "country": "DE"},
           {"email": "b@acme-group.com", "company": "Acme", "country": "DE"})
    rows = db.execute("SELECT domain, company_id FROM company_domains").fetchall()
    got = {(r["domain"], r["company_id"]) for r in rows}
    assert got == {("acme-group.com", 1), ("acme.de", 1)}


def test_same_name_other_country_not_merged(db):
    ingest(db, {"email": "a@gmail.com", "company": "Acme", "country": "DE"},
           {"email": "b@gmail.com", "company": "Acme", "country": "GB"})
    assert count(db, "companies") == 2


def test_lead_without_company_is_unassigned(db):
    ingest(db, {"email": "x@gmail.com"})
    contact = db.execute("SELECT email, company_id FROM contacts").fetchone()
    assert contact == {"email": "x@gmail.com", "company_id": None}
    assert count(db, "companies") == 0
    assert db.execute("SELECT count(*) AS n FROM jobs WHERE kind='enrich'").fetchone()["n"] == 0


def test_enrich_enqueued_for_company(db):
    ingest(db, {"email": "a@acme.de"})
    assert db.execute("SELECT payload FROM jobs WHERE kind='enrich'").fetchone() == {
        "payload": {"company_id": 1}}


def test_contact_upsert_by_email(db):
    ingest(db, {"email": "a@acme.de", "title": "Developer"},
           {"email": "A@ACME.de", "name": "Ann", "title": "CISO"})
    row = db.execute("SELECT name, title, seniority FROM contacts").fetchone()
    assert count(db, "contacts") == 1
    assert row == {"name": "Ann", "title": "CISO", "seniority": "c_level"}


def test_newer_lead_updates_company_fields(db):
    ingest(db, {"email": "a@acme.de", "company": "Acme", "employees": "40"},
           {"email": "b@acme.de", "employees": "60"})
    row = db.execute("SELECT employees, provenance FROM companies").fetchone()
    assert row["employees"] == 60 and row["provenance"]["employees"]["lead"] == 2


def test_reprocessing_is_idempotent(db):
    ingest(db, {"company": "No Email Ltd", "country": "GB"}, {"email": "a@acme.de"})
    db.execute("INSERT INTO jobs (kind, payload) SELECT 'process_lead', "
               "jsonb_build_object('raw_lead_id', id) FROM raw_leads")
    db.commit()
    drain(get_pool(), get_settings())
    assert count(db, "companies") == 2 and count(db, "contacts") == 2


def test_shouty_name_does_not_replace_nicely_cased_one(db):
    ingest(db, {"email": "a@granite.nl", "company": "Granite Autoteile B.V."},
           {"email": "b@granite.nl", "company": "GRANITE AUTOTEILE B.V."},
           {"email": "c@granite.nl", "company": "granite autoteile"})
    assert db.execute("SELECT canonical_name FROM companies").fetchone() == {
        "canonical_name": "Granite Autoteile B.V."}


def test_nicely_cased_name_replaces_shouty_one(db):
    ingest(db, {"email": "a@granite.nl", "company": "GRANITE AUTOTEILE"},
           {"email": "b@granite.nl", "company": "Granite Autoteile"})
    assert db.execute("SELECT canonical_name FROM companies").fetchone() == {
        "canonical_name": "Granite Autoteile"}
