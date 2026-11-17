import json
from dataclasses import replace

import httpx
import pytest

from app.classify import CachedClassifier, Classification
from app.config import get_settings
from app.llm import (
    GeminiClassifier,
    GroqClassifier,
    LLMError,
    make_llm_classifier,
    post_json_with_retry,
)

GOOD = {"industry": "automotive", "signals": ["automotive_supplier"]}
TEXT = "Tier 1 supplier of brake systems to BMW"


def gemini_reply(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}],
            "usageMetadata": {"promptTokenCount": 300, "candidatesTokenCount": 20}}


def groq_reply(text: str) -> dict:
    return {"choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 310, "completion_tokens": 18}}


def client_returning(*responses: httpx.Response, seen: list | None = None) -> httpx.Client:
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return queue.pop(0)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_gemini_parses_valid_reply():
    seen = []
    c = GeminiClassifier("key", "gemini-x",
                         client_returning(httpx.Response(200, json=gemini_reply(json.dumps(GOOD))),
                                          seen=seen))
    assert c.classify(TEXT) == Classification("automotive", frozenset({"automotive_supplier"}))
    req = seen[0]
    assert req.url.path == "/v1beta/models/gemini-x:generateContent"
    assert req.headers["x-goog-api-key"] == "key"
    assert json.loads(req.content)["generationConfig"]["responseMimeType"] == "application/json"
    assert c.usage == {"in": 300, "out": 20} and c.fallbacks == 0


def test_groq_parses_valid_reply():
    seen = []
    c = GroqClassifier("key", "llama-x",
                       client_returning(httpx.Response(200, json=groq_reply(json.dumps(GOOD))),
                                        seen=seen))
    assert c.classify(TEXT).industry == "automotive"
    body = json.loads(seen[0].content)
    assert seen[0].headers["authorization"] == "Bearer key"
    assert body["model"] == "llama-x" and body["response_format"] == {"type": "json_object"}
    assert c.usage == {"in": 310, "out": 18}


@pytest.mark.parametrize("reply", ["not json", json.dumps({"industry": "space", "signals": []})])
def test_invalid_reply_falls_back_to_rules(reply):
    c = GroqClassifier("key", "m", client_returning(httpx.Response(200, json=groq_reply(reply))))
    assert c.classify(TEXT).industry == "automotive"  # rules got it
    assert c.fallbacks == 1 and c.last_was_fallback is True


def test_429_then_success_honours_retry_after():
    sleeps = []
    client = client_returning(httpx.Response(429, headers={"Retry-After": "2"}),
                              httpx.Response(200, json={"ok": True}))
    assert post_json_with_retry(client, "https://x/y", json={}, headers={},
                                sleep=sleeps.append) == {"ok": True}
    assert sleeps == [2]


def test_retry_after_is_capped():
    sleeps = []
    client = client_returning(httpx.Response(503, headers={"Retry-After": "999"}),
                              httpx.Response(200, json={}))
    post_json_with_retry(client, "https://x/y", json={}, headers={}, sleep=sleeps.append)
    assert sleeps == [30]


def test_429_exhausted_falls_back():
    c = GeminiClassifier("key", "m", client_returning(*[httpx.Response(429)] * 3),
                         sleep=lambda s: None)
    assert c.classify(TEXT).industry == "automotive" and c.fallbacks == 1


def test_401_does_not_retry():
    sleeps = []
    client = client_returning(httpx.Response(401, text="bad key"))
    with pytest.raises(LLMError, match="401"):
        post_json_with_retry(client, "https://x/y", json={}, headers={}, sleep=sleeps.append)
    assert sleeps == []


def test_empty_text_skips_the_api():
    c = GroqClassifier("key", "m", client_returning())  # any request would pop an empty list
    assert c.classify("").industry == "other" and c.fallbacks == 0


def test_fallback_is_not_cached_as_llm_answer(db):
    inner = GroqClassifier("key", "m", client_returning(httpx.Response(200, json=groq_reply("x"))))
    CachedClassifier(inner, db).classify(TEXT)
    assert db.execute("SELECT count(*) AS n FROM classify_cache").fetchone()["n"] == 0


def test_missing_key_fails_fast():
    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        make_llm_classifier(replace(get_settings(), llm_provider="groq", groq_api_key=""))
    with pytest.raises(ValueError, match="unknown"):
        make_llm_classifier(replace(get_settings(), llm_provider="gpt"))


def test_prompt_lists_every_label():
    from app.classify import INDUSTRIES, SIGNALS
    from app.llm import PROMPT

    assert all(label in PROMPT for label in (*INDUSTRIES, *SIGNALS))


def test_complete_returns_free_text():
    seen = []
    c = GroqClassifier("key", "m", client_returning(
        httpx.Response(200, json=groq_reply("BRIEF:\nx\nEMAIL:\ny")), seen=seen))
    assert c.complete("write a brief") == "BRIEF:\nx\nEMAIL:\ny"
    assert "response_format" not in json.loads(seen[0].content)
