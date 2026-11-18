"""AE brief agent: an account brief + first email, saved as a DRAFT that a human approves.
Nothing is ever sent. With no LLM (or a bad reply) a deterministic template is used."""

import logging
from collections.abc import Callable

import httpx
import psycopg

from app.agent import research, safe_fetch
from app.score import SENIORITY_RANK

log = logging.getLogger("brief")

# score rule -> (framework, the one-line reason a rep can say out loud)
FRAMEWORKS = {
    "tisax": ("TISAX", "car makers require a TISAX label from their suppliers"),
    "soc2": ("SOC 2", "US enterprise buyers ask for a SOC 2 report in procurement"),
    "eu_hq": ("ISO 27001 / GDPR", "European customers expect ISO 27001 and GDPR evidence"),
    "regulated": ("ISO 27001 / NIS2 / DORA", "regulators expect documented security controls"),
    "hiring_security": (None, "they are hiring for security right now, so the topic is live"),
    "size_fit": (None, "big enough to get security questionnaires, too small for a GRC team"),
    "senior_contact": (None, "the contact is senior enough to buy or champion"),
}


def _best_contact(contacts: list[dict]) -> dict | None:
    return max(contacts, key=lambda c: SENIORITY_RANK.get(c.get("seniority"), 0), default=None)


def _frameworks(breakdown: dict) -> list[str]:
    """Frameworks the score points at, strongest reason (most points) first."""
    ranked = sorted(breakdown.items(), key=lambda item: -item[1])
    return [FRAMEWORKS[r][0] for r, _ in ranked if r in FRAMEWORKS and FRAMEWORKS[r][0]]


def template_brief(company: dict, score: dict, contacts: list[dict]) -> tuple[str, str]:
    name = company["canonical_name"] or "this company"
    frameworks = _frameworks(score["breakdown"]) or ["ISO 27001"]
    reasons = [f"- {FRAMEWORKS[r][1]} ({r} +{pts})"
               for r, pts in sorted(score["breakdown"].items(), key=lambda item: -item[1])
               if r in FRAMEWORKS]
    contact = _best_contact(contacts)
    who = f"{contact['name']} ({contact['title']})" if contact and contact.get("name") else "—"
    brief = "\n".join([
        f"{name}: tier {score['tier']}, score {score['total']}. Owner: {company['owner']}.",
        f"Industry: {company['industry'] or 'unknown'}, {company['country'] or '?'}, "
        f"{company.get('employees') or '?'} employees.",
        f"Likely need: {', '.join(frameworks)}.",
        "Why now:", *reasons,
        f"Best contact: {who}.",
        "Opening angle: ask how they handle security questionnaires today and when their next "
        "audit or customer deadline is.",
    ])
    first = contact["name"].split()[0] if contact and contact.get("name") else "there"
    email = "\n".join([
        f"Subject: {frameworks[0]} without the spreadsheet marathon",
        "",
        f"Hi {first},",
        "",
        f"Teams like {name} usually hit {frameworks[0]} when a big customer asks for it, and "
        "it lands on top of the day job. We automate the evidence collection and policies so "
        "it takes weeks, not quarters.",
        "",
        "Would a 20-minute call next week be useful to see where you'd start?",
    ])
    return brief, email


def _split_reply(reply: str) -> tuple[str, str] | None:
    if "BRIEF:" not in reply or "EMAIL:" not in reply:
        return None
    brief, _, email = reply.partition("BRIEF:")[2].partition("EMAIL:")
    brief, email = brief.strip(), email.strip()
    return (brief, email) if brief and email else None


def make_brief(company: dict, score: dict, contacts: list[dict],
               generate: Callable[[str], str] | None) -> tuple[str, str]:
    """(brief, email_draft). LLM when `generate` is given and its reply has both sections."""
    fallback = template_brief(company, score, contacts)
    if generate is None:
        return fallback
    prompt = (
        "You prepare a sales rep at a compliance-automation company (ISO 27001, SOC 2, TISAX, "
        "GDPR) for a first call. Use only the facts below. Reply in exactly this format:\n"
        "BRIEF:\n<5 short lines: likely framework, why now, best contact, opening question>\n"
        "EMAIL:\nSubject: <subject>\n<under 90 words, plain, no hype>\n\n"
        f"Facts:\n{fallback[0]}"
    )
    try:
        parsed = _split_reply(generate(prompt))
    except Exception:  # any provider failure -> template; the rep still gets a brief
        log.warning("brief generation failed, using template", exc_info=True)
        return fallback
    return parsed or fallback


BRIEF_COLUMNS = "id, company_id, brief, email_draft, status, mode, created_at, approved_at"


def make_agent_llm():
    """The tool-calling model for the research agent, or None (template) when offline or in
    the public demo. Groq: OpenAI-style tool calling on the free tier."""
    from app.config import get_settings
    from app.llm import GroqClassifier

    settings = get_settings()
    if settings.demo_mode or not settings.groq_api_key:
        return None
    return GroqClassifier(settings.groq_api_key, settings.groq_model)


def _with_steps(conn: psycopg.Connection, row: dict) -> dict:
    steps = conn.execute("SELECT tool, input, result, ok FROM agent_steps WHERE brief_id = %s "
                         "ORDER BY n", (row["id"],)).fetchall()
    return row | {"steps": steps}


def create_brief(conn: psycopg.Connection, company_id: int) -> dict | None:
    company = conn.execute("SELECT canonical_name, country, employees, industry, owner "
                           "FROM companies WHERE id = %s", (company_id,)).fetchone()
    score = conn.execute("SELECT total, tier, breakdown FROM scores WHERE company_id = %s",
                         (company_id,)).fetchone()
    if company is None or score is None:
        return None
    contacts = conn.execute("SELECT name, title, seniority, email FROM contacts "
                            "WHERE company_id = %s", (company_id,)).fetchall()
    domains = [r["domain"] for r in conn.execute(
        "SELECT domain FROM company_domains WHERE company_id = %s ORDER BY domain",
        (company_id,))]
    llm, steps, mode = make_agent_llm(), [], "template"
    if llm is None:
        brief, email = make_brief(company, score, contacts, generate=None)
    else:
        with httpx.Client() as client:
            brief, email, steps = research(
                company, score, contacts, domains, llm,
                lambda url: safe_fetch(url, domains, client))
        mode = "agent"
    row = conn.execute(
        f"INSERT INTO briefs (company_id, brief, email_draft, mode) VALUES (%s, %s, %s, %s) "
        f"RETURNING {BRIEF_COLUMNS}", (company_id, brief, email, mode)).fetchone()
    for n, step in enumerate(steps):
        conn.execute("INSERT INTO agent_steps (brief_id, n, tool, input, result, ok) "
                     "VALUES (%s, %s, %s, %s, %s, %s)",
                     (row["id"], n, step["tool"], step["input"], step["result"], step["ok"]))
    return _with_steps(conn, row)


def get_brief(conn: psycopg.Connection, brief_id: int) -> dict | None:
    row = conn.execute(f"SELECT {BRIEF_COLUMNS} FROM briefs WHERE id = %s",
                       (brief_id,)).fetchone()
    return _with_steps(conn, row) if row else None


def approve_brief(conn: psycopg.Connection, brief_id: int) -> dict | None:
    """Idempotent: approving twice keeps the first approval time."""
    conn.execute("UPDATE briefs SET status = 'approved', approved_at = now() "
                 "WHERE id = %s AND status = 'draft'", (brief_id,))
    return get_brief(conn, brief_id)
