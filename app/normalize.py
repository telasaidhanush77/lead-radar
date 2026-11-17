"""Turn a messy inbound lead into clean, comparable fields. Pure functions, no I/O."""

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit

FREE_MAIL = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com", "gmx.de",
    "gmx.net", "web.de", "icloud.com", "proton.me", "protonmail.com", "t-online.de",
    "orange.fr", "libero.it", "live.com",
})

LEGAL_SUFFIXES = frozenset({
    "gmbh", "ag", "ug", "kg", "ltd", "limited", "plc", "bv", "nv", "sa", "sas", "sarl", "srl",
    "spa", "ab", "as", "oy", "aps", "inc", "llc", "co", "se", "eg", "sl", "pvt",
    "sp", "z", "oo",  # Polish "Sp. z o.o."
})

_COUNTRIES = {
    "AT": ["austria", "osterreich", "oesterreich"], "BE": ["belgium", "belgie", "belgique"],
    "BG": ["bulgaria"], "HR": ["croatia", "hrvatska"], "CY": ["cyprus"],
    "CZ": ["czechia", "czech republic", "cesko"], "DK": ["denmark", "danmark"],
    "EE": ["estonia", "eesti"], "FI": ["finland", "suomi"], "FR": ["france"],
    "DE": ["germany", "deutschland"], "GR": ["greece", "hellas"],
    "HU": ["hungary", "magyarorszag"], "IE": ["ireland", "eire"], "IT": ["italy", "italia"],
    "LV": ["latvia", "latvija"], "LT": ["lithuania", "lietuva"], "LU": ["luxembourg"],
    "MT": ["malta"], "NL": ["netherlands", "the netherlands", "nederland", "holland"],
    "PL": ["poland", "polska"], "PT": ["portugal"], "RO": ["romania"],
    "SK": ["slovakia", "slovensko"], "SI": ["slovenia", "slovenija"],
    "ES": ["spain", "espana"], "SE": ["sweden", "sverige"],
    "GB": ["united kingdom", "uk", "great britain", "england", "scotland", "wales"],
    "CH": ["switzerland", "schweiz", "suisse", "svizzera"], "NO": ["norway", "norge"],
    "IS": ["iceland", "island"], "LI": ["liechtenstein"],
    "US": ["united states", "usa", "united states of america"], "IN": ["india", "bharat"],
}
COUNTRY_LOOKUP = {code.lower(): code for code in _COUNTRIES} | {
    name: code for code, names in _COUNTRIES.items() for name in names
}

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue"})

# Order matters: the first matching rule wins.
_SENIORITY = [
    ("c_level", r"\bchief\b|^c[a-z]{1,3}o\b|\bfounder\b|\bowner\b|geschaeftsfuehrer"
                r"|managing director|\bpresident\b"),
    ("vp", r"\bvp\b|vice president"),
    ("director", r"\bdirector\b|\bhead of\b|\bleiter\b"),
    ("manager", r"\bmanager\b|\blead\b"),
]
_DEPARTMENT = [
    ("security", r"secur|ciso|infosec|complian|privacy|\bdpo\b|\bgrc\b"),
    ("engineering", r"engineer|\bcto\b|develop|devops"),
    ("it", r"\bit\b|information technology|\bcio\b|sysadmin"),
    ("sales", r"sales|account|\bbdr\b|\bsdr\b|revenue"),
    ("finance", r"\bcfo\b|financ|accounting|controller"),
    ("ops", r"\bcoo\b|operations|\bops\b"),
]


@dataclass(frozen=True)
class NormalizedLead:
    email: str | None
    name: str | None
    title: str | None
    seniority: str
    department: str
    company_name: str | None
    name_key: str | None
    domain: str | None
    free_mail: bool
    country: str | None
    employees: int | None
    description: str | None


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def fold(text: str) -> str:
    """Casefold, spell umlauts as ae/oe/ue (German convention), then strip other accents."""
    text = text.casefold().translate(_UMLAUTS)  # casefold already turns ß into ss
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def normalize_email(value: object) -> str | None:
    email = _clean(value)
    if email is None:
        return None
    email = email.lower()
    return email if _EMAIL.match(email) else None


def _host(website: str) -> str | None:
    if "//" not in website:
        website = "//" + website
    host = urlsplit(website.strip()).hostname  # lowercases and drops the port
    if not host or "." not in host:
        return None
    return host.removeprefix("www.")


def domain_from(email: str | None, website: str | None) -> tuple[str | None, bool]:
    """Company domain plus whether the email was free-mail. Email domain wins unless free-mail."""
    email = normalize_email(email)
    free_mail = False
    if email:
        domain = email.rsplit("@", 1)[1]
        if domain not in FREE_MAIL:
            return domain, False
        free_mail = True
    site = _clean(website)
    host = _host(site) if site else None
    if host in FREE_MAIL:
        host = None
    return host, free_mail


def name_key(name: str | None) -> str | None:
    """'Müller-Automotive GmbH' -> 'mueller automotive'. The key fuzzy matching compares.
    Dots and slashes go first so 'S.A.' -> 'sa' and 'A/S' -> 'as' are recognised suffixes."""
    name = _clean(name)
    if name is None:
        return None
    text = re.sub(r"[^a-z0-9]+", " ", fold(name).replace(".", "").replace("/", ""))
    tokens = [t for t in text.split() if t not in LEGAL_SUFFIXES]
    return " ".join(tokens) or None


def country_code(value: str | None) -> str | None:
    value = _clean(value)
    return COUNTRY_LOOKUP.get(fold(value)) if value else None


def _first_match(rules: list[tuple[str, str]], title: str | None, default: str) -> str | None:
    if not title:
        return None
    text = fold(title)
    for label, pattern in rules:
        if re.search(pattern, text):
            return label
    return default


def seniority(title: str | None) -> str:
    return _first_match(_SENIORITY, title, "ic") or "unknown"


def department(title: str | None) -> str:
    return _first_match(_DEPARTMENT, title, "other") or "other"


def parse_employees(value: object) -> int | None:
    """'1,200' -> 1200, '50-200' -> 125 (midpoint). Anything vaguer -> None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    text = (_clean(value) or "").replace(",", "").replace(" ", "")
    if m := re.fullmatch(r"(\d+)-(\d+)", text):
        return (int(m[1]) + int(m[2])) // 2
    return int(text) if text.isdigit() else None


def normalize_lead(payload: dict) -> NormalizedLead:
    email = normalize_email(payload.get("email"))
    title = _clean(payload.get("title"))
    company = _clean(payload.get("company"))
    domain, free_mail = domain_from(email, payload.get("website"))
    return NormalizedLead(
        email=email,
        name=_clean(payload.get("name")),
        title=title,
        seniority=seniority(title),
        department=department(title),
        company_name=company,
        name_key=name_key(company),
        domain=domain,
        free_mail=free_mail,
        country=country_code(payload.get("country")),
        employees=parse_employees(payload.get("employees")),
        description=_clean(payload.get("description")),
    )
