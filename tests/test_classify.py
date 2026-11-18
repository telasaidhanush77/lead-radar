from dataclasses import replace

import pytest

from app.classify import (
    CachedClassifier,
    Classification,
    RulesClassifier,
    enrich,
    get_classifier,
    validate,
)
from app.config import get_settings

rules = RulesClassifier()


def test_rules_automotive_supplier():
    c = rules.classify("Tier-1 Zulieferer for German OEMs, making brake systems.")
    assert c.industry == "automotive" and "automotive_supplier" in c.signals


def test_rules_us_enterprise():
    c = rules.classify("B2B SaaS platform; half our customers are US enterprises (Fortune 500).")
    assert c.industry == "saas" and "us_enterprise_customers" in c.signals


def test_rules_hiring_security():
    c = rules.classify("Fast-growing logistics startup, currently hiring a Head of Security.")
    assert c.industry == "logistics" and c.signals == {"hiring_security"}


def test_rules_regulated_fintech():
    c = rules.classify("BaFin-licensed payment provider for small merchants.")
    assert c.industry == "fintech" and "regulated_industry" in c.signals


def test_rules_empty_text_other():
    assert rules.classify("") == Classification("other", frozenset())


def test_validate_accepts_good_reply():
    assert validate({"industry": "saas", "signals": ["hiring_security"]}) == Classification(
        "saas", frozenset({"hiring_security"}))


@pytest.mark.parametrize("bad", [
    {"industry": "space", "signals": []},
    {"industry": "saas", "signals": ["made_up"]},
    {"industry": "saas", "signals": "hiring_security"},
    {"industry": 3, "signals": []},
    {"signals": []},
    ["saas"],
])
def test_validate_rejects_bad_reply(bad):
    with pytest.raises(ValueError):
        validate(bad)


class CountingClassifier:
    name = "counting"

    def __init__(self):
        self.calls = 0

    def classify(self, text):
        self.calls += 1
        return Classification("saas", frozenset())


def test_cache_hit_skips_inner(db):
    inner = CountingClassifier()
    cached = CachedClassifier(inner, db)
    assert cached.classify("same text") == cached.classify("same text")
    assert inner.calls == 1
    assert db.execute("SELECT provider FROM classify_cache").fetchone() == {"provider": "counting"}


def test_get_classifier_default_is_cached_rules(db):
    c = get_classifier(get_settings(), db)
    assert c.name == "rules" and isinstance(c, CachedClassifier)


def test_demo_mode_never_calls_network(db):
    db.execute("INSERT INTO classify_cache (provider, content_hash, result) VALUES "
               "('gemini', encode(sha256('cached text'::bytea), 'hex'), "
               "'{\"industry\": \"fintech\", \"signals\": []}')")
    settings = replace(get_settings(), llm_provider="gemini", gemini_api_key="", demo_mode=True)
    c = get_classifier(settings, db)
    assert c.classify("cached text").industry == "fintech"               # served from cache
    assert c.classify("Tier 1 Zulieferer").industry == "automotive"      # miss -> rules


def test_enrich_writes_and_enqueues_score(db):
    db.execute("INSERT INTO companies (description) VALUES ('Tier-1 supplier to BMW and Audi')")
    enrich(db, {"company_id": 1}, get_settings())
    row = db.execute("SELECT industry, signals FROM companies").fetchone()
    assert row == {"industry": "automotive", "signals": ["automotive_supplier"]}
    assert db.execute("SELECT kind, payload FROM jobs").fetchone() == {
        "kind": "score", "payload": {"company_id": 1}}


def test_rules_tisax_means_automotive_supplier():
    c = rules.classify("Currently hiring: Informationssicherheitsbeauftragter (TISAX).")
    assert c.industry == "automotive" and "automotive_supplier" in c.signals
    assert "hiring_security" in c.signals


def test_rules_currently_hiring_isms_role():
    c = rules.classify("Currently hiring: ISMS Manager ISO 27001.")
    assert "hiring_security" in c.signals


def test_enrich_uses_every_open_role_not_just_the_newest(db):
    from psycopg.types.json import Jsonb

    db.execute("INSERT INTO companies (description) "
               "VALUES ('Currently hiring: ISO 27001 Auditor.')")
    for i, title in enumerate(["ISMS Coordinator (TISAX)", "ISO 27001 Auditor"]):
        db.execute("INSERT INTO contacts (company_id) VALUES (1)")
        db.execute("INSERT INTO raw_leads (source, idempotency_key, payload, contact_id) "
                   "VALUES ('adzuna', %s, %s, %s)",
                   (f"k{i}", Jsonb({"signal": {"title": title}}), i + 1))
    enrich(db, {"company_id": 1}, get_settings())
    signals = db.execute("SELECT signals FROM companies").fetchone()["signals"]
    assert "automotive_supplier" in signals and "hiring_security" in signals
