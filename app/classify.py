"""Company description -> industry + compliance signals. One interface, several engines."""

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Protocol

import psycopg
from psycopg.types.json import Jsonb

from app.config import Settings
from app.normalize import fold
from app.queue import enqueue

INDUSTRIES = ("automotive", "fintech", "healthtech", "saas", "manufacturing", "ecommerce",
              "logistics", "other")
SIGNALS = ("automotive_supplier", "us_enterprise_customers", "hiring_security",
           "regulated_industry")

_CARMAKERS = r"bmw|volkswagen|\bvw\b|mercedes|daimler|audi|porsche|stellantis|renault|volvo cars"

# First match wins, so specific industries come before broad ones (automotive before
# manufacturing, everything before the catch-all 'saas' keywords like 'platform').
_INDUSTRY_RULES = [
    ("automotive", rf"automotive|\boems?\b|vehicle|fahrzeug|\bkfz\b|tier[- ]?[12]\b|zulieferer"
                   rf"|car parts|auto parts|brake|chassis|{_CARMAKERS}"),
    ("fintech", r"fintech|payment|banking|\bbank|lending|neobank|bafin|insurtech|crypto"
                r"|wealth management|zahlungs"),
    ("healthtech", r"health|hospital|krankenhaus|klinik|clinic|patient|medical|medtech|pharma"
                   r"|telemedicine|ziekenhuis"),
    ("logistics", r"logistic|logistik|freight|shipping|warehous|fleet|supply chain|spedition"),
    ("ecommerce", r"e-?commerce|online shop|web ?shop|marketplace|retail|d2c"),
    ("manufacturing", r"manufactur|factory|industrial|machinery|maschinenbau|production plant"
                      r"|fabrik"),
    ("saas", r"saas|software|platform|cloud|\bapi\b|plattform"),
]
_SIGNAL_RULES = {
    "automotive_supplier": rf"zulieferer|tier[- ]?[12]\b|\boems?\b|automotive supplier"
                           rf"|supplier (to|for|of) .*({_CARMAKERS}|car|automotive)"
                           rf"|supplies .*({_CARMAKERS})",
    "us_enterprise_customers": r"fortune 500|\b(us|u\.s\.|american|north american) "
                               r"(enterprise|customers|clients|market)|soc ?2|expanding to the us"
                               r"|customers in the (us|united states)",
    "hiring_security": r"hiring .*(security|ciso|compliance|infosec)"
                       r"|(security|compliance) (engineer|officer|manager|lead)s? .*(open|hiring)"
                       r"|looking for an? .*(security|compliance|ciso)",
    "regulated_industry": r"bafin|regulated|\bdora\b|nis ?2|hipaa|\bbank|insurance|versicherung"
                          r"|health data|patient data|medical device|critical infrastructure"
                          r"|kritis|licensed",
}


@dataclass(frozen=True)
class Classification:
    industry: str
    signals: frozenset[str]

    def to_json(self) -> dict:
        return {"industry": self.industry, "signals": sorted(self.signals)}


def validate(data: object) -> Classification:
    """Strictly check an untrusted (LLM) reply. Anything off -> ValueError."""
    if not isinstance(data, dict):
        raise ValueError("reply is not an object")
    industry, signals = data.get("industry"), data.get("signals")
    if industry not in INDUSTRIES:
        raise ValueError(f"unknown industry {industry!r}")
    if not isinstance(signals, list) or not all(s in SIGNALS for s in signals):
        raise ValueError(f"bad signals {signals!r}")
    return Classification(industry, frozenset(signals))


class Classifier(Protocol):
    name: str

    def classify(self, text: str) -> Classification: ...


class RulesClassifier:
    name = "rules"

    def classify(self, text: str) -> Classification:
        t = fold(text or "")
        industry = next((label for label, rx in _INDUSTRY_RULES if re.search(rx, t)), "other")
        signals = frozenset(s for s, rx in _SIGNAL_RULES.items() if re.search(rx, t))
        return Classification(industry, signals)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class CachedClassifier:
    """Same text, same provider -> answered from Postgres. Re-runs are free and offline."""

    def __init__(self, inner: Classifier, conn: psycopg.Connection):
        self.inner, self.conn, self.name = inner, conn, inner.name

    def classify(self, text: str) -> Classification:
        key = content_hash(text)
        row = self.conn.execute(
            "SELECT result FROM classify_cache WHERE provider = %s AND content_hash = %s",
            (self.name, key)).fetchone()
        if row:
            return validate(row["result"])
        start = time.perf_counter()
        result = self.inner.classify(text)
        self.conn.execute(
            "INSERT INTO classify_cache (provider, content_hash, result, latency_ms) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (self.name, key, Jsonb(result.to_json()), int((time.perf_counter() - start) * 1000)))
        return result


class CacheOnlyClassifier:
    """Public demo: replay a provider's cached answers, never call the network."""

    def __init__(self, provider: str, conn: psycopg.Connection):
        self.name, self.conn, self.fallback = provider, conn, RulesClassifier()

    def classify(self, text: str) -> Classification:
        row = self.conn.execute(
            "SELECT result FROM classify_cache WHERE provider = %s AND content_hash = %s",
            (self.name, content_hash(text))).fetchone()
        return validate(row["result"]) if row else self.fallback.classify(text)


def get_classifier(settings: Settings, conn: psycopg.Connection) -> Classifier:
    provider = settings.llm_provider
    if settings.demo_mode and provider != "rules":
        return CacheOnlyClassifier(provider, conn)
    if provider == "rules":
        return CachedClassifier(RulesClassifier(), conn)
    from app.llm import make_llm_classifier  # imported lazily: rules mode needs no httpx client

    return CachedClassifier(make_llm_classifier(settings), conn)


def enrich(conn: psycopg.Connection, payload: dict, settings: Settings) -> None:
    """Job handler: classify the company's description, then queue scoring."""
    company_id = payload["company_id"]
    row = conn.execute("SELECT description FROM companies WHERE id = %s",
                       (company_id,)).fetchone()
    result = get_classifier(settings, conn).classify(row["description"] or "")
    conn.execute("UPDATE companies SET industry = %s, signals = %s, updated_at = now() "
                 "WHERE id = %s",
                 (result.industry, Jsonb(sorted(result.signals)), company_id))
    enqueue(conn, "score", {"company_id": company_id})
