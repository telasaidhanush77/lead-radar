import pytest

from app.config import get_settings
from app.score import WEIGHTS, score, score_company, tier_for

ALL_SIGNALS = ["automotive_supplier", "us_enterprise_customers", "hiring_security",
               "regulated_industry"]


def test_full_house_is_125_tier_a():
    total, tier, breakdown = score("DE", 200, ALL_SIGNALS, "c_level")
    assert total == 125 == sum(WEIGHTS.values())
    assert tier == "A" and breakdown == WEIGHTS


def test_nothing_fires_zero_tier_c():
    assert score(None, None, [], "unknown") == (0, "C", {})


@pytest.mark.parametrize("total,tier", [(69, "B"), (70, "A"), (39, "C"), (40, "B"), (0, "C")])
def test_tier_boundaries(total, tier):
    assert tier_for(total) == tier


@pytest.mark.parametrize("employees,fires", [(19, False), (20, True), (500, True), (501, False),
                                             (None, False)])
def test_size_bounds(employees, fires):
    assert ("size_fit" in score("US", employees, [], "ic")[2]) is fires


def test_europe_includes_uk_and_switzerland_not_us():
    assert "eu_hq" in score("GB", None, [], "ic")[2]
    assert "eu_hq" in score("CH", None, [], "ic")[2]
    assert "eu_hq" not in score("US", None, [], "ic")[2]


def test_breakdown_only_fired_rules():
    total, tier, breakdown = score("DE", 5000, ["automotive_supplier"], "manager")
    assert breakdown == {"eu_hq": 15, "tisax": 30} and total == 45 and tier == "B"


@pytest.mark.parametrize("seniority,fires", [("c_level", True), ("vp", True), ("director", True),
                                             ("manager", False), ("ic", False)])
def test_senior_contact(seniority, fires):
    assert ("senior_contact" in score(None, None, [], seniority)[2]) is fires


def test_handler_uses_most_senior_contact_and_is_idempotent(db):
    db.execute("INSERT INTO companies (country, employees, signals) "
               "VALUES ('DE', 100, '[\"hiring_security\"]')")
    db.execute("INSERT INTO contacts (email, seniority, company_id) VALUES "
               "('a@x.de', 'ic', 1), ('b@x.de', 'vp', 1)")
    score_company(db, {"company_id": 1}, get_settings())
    score_company(db, {"company_id": 1}, get_settings())
    rows = db.execute("SELECT total, tier, breakdown, model_version FROM scores").fetchall()
    assert rows == [{"total": 60, "tier": "B", "model_version": "rules-v1", "breakdown": {
        "eu_hq": 15, "size_fit": 15, "hiring_security": 20, "senior_contact": 10}}]
    kinds = [r["kind"] for r in db.execute("SELECT kind FROM jobs")]
    assert kinds == ["route", "route"]
