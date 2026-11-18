import json
import socket

import httpx
import pytest

from app.agent import FetchError, research, safe_fetch

COMPANY = {"canonical_name": "Müller Automotive GmbH", "country": "DE", "employees": 250,
           "industry": "automotive", "owner": "anna"}
SCORE = {"total": 90, "tier": "A", "breakdown": {"tisax": 30, "eu_hq": 15}}
CONTACTS = [{"name": "Jan Weber", "title": "CISO", "seniority": "c_level", "email": "j@m.de"}]
DOMAINS = ["mueller-auto.de"]


def public_dns(host, *args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


def private_dns(host, *args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]


def site(pages: dict[str, tuple[int, str]]) -> httpx.Client:
    def handler(request):
        status, body = pages.get(str(request.url), (404, "not found"))
        return httpx.Response(status, text=body, headers={"content-type": "text/html"})
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---- safe_fetch ---------------------------------------------------------------------------
def test_fetch_returns_readable_text():
    client = site({"https://www.mueller-auto.de/careers":
                   (200, "<html><script>x()</script><h1>Jobs</h1><p>CISO (m/w/d)</p></html>")})
    text = safe_fetch("https://www.mueller-auto.de/careers", DOMAINS, client, resolve=public_dns)
    assert "Jobs" in text and "CISO (m/w/d)" in text and "x()" not in text


@pytest.mark.parametrize("url", [
    "https://evil.com/", "https://mueller-auto.de.evil.com/", "ftp://mueller-auto.de/",
    "file:///etc/passwd", "https://user@evil.com/", "http://169.254.169.254/latest/meta-data",
])
def test_fetch_refuses_other_hosts_and_schemes(url):
    with pytest.raises(FetchError):
        safe_fetch(url, DOMAINS, site({}), resolve=public_dns)


def test_fetch_refuses_private_ip():
    with pytest.raises(FetchError, match="non-public"):
        safe_fetch("https://mueller-auto.de/", DOMAINS, site({}), resolve=private_dns)


def redirecting(location: str, final: str = "<p>Home</p>") -> httpx.Client:
    def handler(request):
        if str(request.url) == "https://mueller-auto.de/":
            return httpx.Response(301, headers={"location": location})
        return httpx.Response(200, text=final, headers={"content-type": "text/html"})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_same_site_redirect_is_followed():
    client = redirecting("https://www.mueller-auto.de/en/")
    assert safe_fetch("https://mueller-auto.de/", DOMAINS, client, resolve=public_dns) == "Home"


def test_relative_redirect_is_followed():
    assert safe_fetch("https://mueller-auto.de/", DOMAINS, redirecting("/en/"),
                      resolve=public_dns) == "Home"


def test_off_site_redirect_is_refused():
    with pytest.raises(FetchError, match="host not allowed"):
        safe_fetch("https://mueller-auto.de/", DOMAINS, redirecting("https://evil.com/"),
                   resolve=public_dns)


def test_redirect_loop_stops():
    def handler(request):
        return httpx.Response(302, headers={"location": "https://mueller-auto.de/"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(FetchError, match="too many redirects"):
        safe_fetch("https://mueller-auto.de/", DOMAINS, client, resolve=public_dns)


def test_fetch_truncates_to_limit():
    client = site({"https://mueller-auto.de/": (200, "<p>" + "a" * 20000 + "</p>")})
    assert len(safe_fetch("https://mueller-auto.de/", DOMAINS, client,
                          resolve=public_dns)) <= 4000


# ---- the loop -----------------------------------------------------------------------------
class ScriptedLLM:
    """Plays back assistant turns; records what the agent sent."""

    def __init__(self, turns):
        self.turns, self.seen, self.tools_offered = list(turns), [], []

    def chat_tools(self, messages, tools):
        self.seen.append(json.loads(json.dumps(messages)))
        self.tools_offered.append([t["function"]["name"] for t in tools])
        return self.turns.pop(0)


def call(name, args, cid="c1"):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": cid, "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}]}


def fetcher(pages):
    def fetch(url):
        if url not in pages:
            raise FetchError(f"{url}: HTTP 404")
        return pages[url]
    return fetch


def test_agent_researches_then_finishes():
    llm = ScriptedLLM([
        call("fetch_page", {"url": "https://mueller-auto.de/"}, "a"),
        call("fetch_page", {"url": "https://mueller-auto.de/careers"}, "b"),
        call("finish", {"brief": "Supplies BMW; hiring a CISO. Lead with TISAX.",
                        "email": "Subject: TISAX\n\nHi Jan, ..."}, "c"),
    ])
    pages = {"https://mueller-auto.de/": "Brake systems for BMW",
             "https://mueller-auto.de/careers": "We are hiring a CISO"}
    brief, email, steps = research(COMPANY, SCORE, CONTACTS, DOMAINS, llm, fetcher(pages))
    assert brief.startswith("Supplies BMW") and email.startswith("Subject: TISAX")
    assert [(s["tool"], s["ok"]) for s in steps] == [
        ("fetch_page", True), ("fetch_page", True), ("finish", True)]
    assert "We are hiring a CISO" in json.dumps(llm.seen[-1])   # tool results fed back
    assert "untrusted" in llm.seen[0][0]["content"].lower()    # injection warning in prompt


def test_failed_fetch_is_reported_to_the_model_not_fatal():
    llm = ScriptedLLM([
        call("fetch_page", {"url": "https://evil.com/"}, "a"),
        call("finish", {"brief": "b", "email": "Subject: s\n\nHi"}, "b"),
    ])

    def fetch(url):
        raise FetchError("host not allowed")

    _, _, steps = research(COMPANY, SCORE, CONTACTS, DOMAINS, llm, fetch)
    assert steps[0]["ok"] is False and "not allowed" in steps[0]["result"]
    assert "not allowed" in json.dumps(llm.seen[1])


def test_step_limit_falls_back_to_template():
    llm = ScriptedLLM([call("fetch_page", {"url": "https://mueller-auto.de/"}, str(i))
                       for i in range(10)])
    brief, email, steps = research(COMPANY, SCORE, CONTACTS, DOMAINS, llm,
                                   fetcher({"https://mueller-auto.de/": "hi"}))
    assert "TISAX" in brief and email.startswith("Subject:")   # template
    assert len(steps) == 6 and steps[-1]["tool"] == "fetch_page"


def test_bad_tool_call_falls_back():
    llm = ScriptedLLM([{"role": "assistant", "content": "I think they need TISAX."}])
    brief, _, steps = research(COMPANY, SCORE, CONTACTS, DOMAINS, llm, fetcher({}))
    assert "TISAX" in brief and steps == []


def test_llm_error_falls_back_with_steps_kept():
    class Boom(ScriptedLLM):
        def chat_tools(self, messages, tools):
            if self.turns:
                return super().chat_tools(messages, tools)
            raise RuntimeError("HTTP 429")

    llm = Boom([call("fetch_page", {"url": "https://mueller-auto.de/"})])
    brief, _, steps = research(COMPANY, SCORE, CONTACTS, DOMAINS, llm,
                               fetcher({"https://mueller-auto.de/": "hi"}))
    assert "TISAX" in brief and len(steps) == 1


def test_no_domains_means_no_fetch_tool():
    llm = ScriptedLLM([call("finish", {"brief": "b", "email": "Subject: s\n\nHi"})])
    research(COMPANY, SCORE, CONTACTS, [], llm, fetcher({}))
    assert "no website" in llm.seen[0][1]["content"].lower()


def test_three_failed_fetches_in_a_row_stops_early():
    llm = ScriptedLLM([call("fetch_page", {"url": f"https://mueller-auto.de/{i}"}, str(i))
                       for i in range(6)])
    brief, _, steps = research(COMPANY, SCORE, CONTACTS, DOMAINS, llm, fetcher({}))
    assert len(steps) == 3 and len(llm.seen) == 3 and "TISAX" in brief


def test_last_turn_offers_only_finish():
    llm = ScriptedLLM([call("fetch_page", {"url": "https://mueller-auto.de/"}, str(i))
                       for i in range(10)])
    research(COMPANY, SCORE, CONTACTS, DOMAINS, llm, fetcher({"https://mueller-auto.de/": "hi"}))
    assert llm.tools_offered[0] == ["fetch_page", "finish"]
    assert llm.tools_offered[-1] == ["finish"]
