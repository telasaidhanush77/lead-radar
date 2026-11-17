import pytest

from app.normalize import (
    country_code,
    department,
    domain_from,
    name_key,
    normalize_lead,
    parse_employees,
    seniority,
)


@pytest.mark.parametrize(
    "raw", ["Müller Automotive GmbH", "MUELLER Automotive", "Müller-Automotive AG"]
)
def test_name_key_folds_umlauts_and_suffixes(raw):
    assert name_key(raw) == "mueller automotive"


def test_name_key_societe_sa():
    assert name_key("Société Générale S.A.") == "societe generale"


def test_name_key_only_suffix_is_none():
    assert name_key("GmbH") is None
    assert name_key("  ") is None
    assert name_key(None) is None


def test_free_mail_falls_back_to_website():
    assert domain_from("jane@gmail.com", "https://www.acme.de/about") == ("acme.de", True)


def test_free_mail_without_website():
    assert domain_from("jane@gmail.com", None) == (None, True)


def test_company_email_wins_over_website():
    assert domain_from("Jane@Acme.DE", "acme-group.com") == ("acme.de", False)


def test_website_with_port_and_no_scheme():
    assert domain_from(None, "WWW.Acme.nl:8080/x") == ("acme.nl", False)


@pytest.mark.parametrize("raw", ["Germany", "deutschland", "DE", "de "])
def test_country_code_germany(raw):
    assert country_code(raw) == "DE"


def test_country_code_unknown():
    assert country_code("Narnia") is None
    assert country_code(None) is None


@pytest.mark.parametrize("title,expected", [
    ("CISO", "c_level"), ("Chief Technology Officer", "c_level"), ("Geschäftsführer", "c_level"),
    ("VP Engineering", "vp"), ("Head of IT", "director"), ("Director of Security", "director"),
    ("IT Manager", "manager"), ("Developer", "ic"), (None, "unknown"),
])
def test_seniority(title, expected):
    assert seniority(title) == expected


@pytest.mark.parametrize("title,expected", [
    ("CISO", "security"), ("Information Security Lead", "security"),
    ("VP Engineering", "engineering"), ("Head of IT", "it"), ("Account Executive", "sales"),
    ("CFO", "finance"), ("COO", "ops"), (None, "other"),
])
def test_department(title, expected):
    assert department(title) == expected


@pytest.mark.parametrize("raw,expected", [
    ("200", 200), ("1,200", 1200), ("50-200", 125), (75, 75), ("about 40", None), (None, None),
])
def test_employees_parsing(raw, expected):
    assert parse_employees(raw) == expected


def test_normalize_lead_full():
    lead = normalize_lead({
        "email": " Jan@Mueller-Auto.de ", "name": "Jan Weber", "title": "CISO",
        "company": "Müller Automotive GmbH", "country": "Germany", "employees": "250",
        "description": "Tier-1 supplier", "unknown_field": "ignored",
    })
    assert lead.email == "jan@mueller-auto.de"
    assert lead.domain == "mueller-auto.de" and lead.free_mail is False
    assert lead.name_key == "mueller automotive"
    assert (lead.country, lead.employees, lead.seniority) == ("DE", 250, "c_level")


def test_normalize_lead_empty_payload():
    lead = normalize_lead({})
    assert lead.email is None and lead.domain is None and lead.name_key is None
    assert lead.free_mail is False
    assert lead.seniority == "unknown"


def test_invalid_email_is_none():
    assert normalize_lead({"email": "not-an-email"}).email is None
