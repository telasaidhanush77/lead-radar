"""Gemini and Groq classifiers on free tiers. Any failure falls back to the rules engine,
so a rate limit or a bad reply never stops the pipeline."""

import json
import logging
import time
from collections.abc import Callable

import httpx

from app.classify import INDUSTRIES, SIGNALS, Classification, RulesClassifier, validate
from app.config import Settings

log = logging.getLogger("llm")

RETRYABLE = {429, 500, 502, 503, 504}
MAX_WAIT_S = 30
TIMEOUT_S = 30

SIGNAL_HINTS = {
    "automotive_supplier": "supplies parts, software or services to car makers (OEMs). TISAX.",
    "us_enterprise_customers": "sells to, or is expanding to, US enterprise customers. SOC 2.",
    "hiring_security": "is hiring security, compliance or CISO roles.",
    "regulated_industry": "operates under financial, health, insurance or critical-infra rules.",
}
assert set(SIGNAL_HINTS) == set(SIGNALS), "every signal needs a hint for the LLM"

PROMPT = (
    "You label B2B companies for a compliance-software sales team.\n"
    "Read the company description and answer with JSON only, exactly this shape:\n"
    '{"industry": "<one of: ' + ", ".join(INDUSTRIES) + '>", "signals": [<zero or more>]}\n\n'
    "Signals:\n" + "".join(f"- {k}: {v}\n" for k, v in SIGNAL_HINTS.items())
    + '\nUse "other" when no industry fits. Only list signals the text supports.\n\n'
    "Description:\n"
)


class LLMError(Exception):
    pass


def _wait_s(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After", "")
    return min(int(retry_after), MAX_WAIT_S) if retry_after.isdigit() else 2**attempt


def post_json_with_retry(client: httpx.Client, url: str, *, json: dict, headers: dict,
                         params: dict | None = None, max_tries: int = 3,
                         sleep: Callable[[float], None] = time.sleep,
                         method: str = "POST", idempotent: bool = True) -> dict:
    """Send and return the JSON body. Retries 429/5xx and network errors, honouring
    Retry-After (capped at 30 s); other 4xx fail at once (retrying a bad key won't help).
    idempotent=False (e.g. a create): only 429 is retried, because after a 5xx or a timeout
    the server may already have done it, and a retry would do it twice."""
    for attempt in range(1, max_tries + 1):
        try:
            response = client.request(method, url, json=json, headers=headers, params=params)
        except httpx.TransportError as exc:
            if attempt == max_tries or not idempotent:
                raise LLMError(f"network: {exc}") from exc
            sleep(2**attempt)
            continue
        if response.status_code < 400:
            return response.json() if response.content else {}
        error = f"HTTP {response.status_code}: {response.text[:200]}"
        status = response.status_code
        retryable = status == 429 or (idempotent and status in RETRYABLE)
        if not retryable or attempt == max_tries:
            raise LLMError(error)
        sleep(_wait_s(response, attempt))
    raise LLMError("unreachable")


class _LLMClassifier:
    """Shared flow: ask -> parse JSON -> validate, or fall back to rules and count it."""

    name = ""

    def __init__(self, api_key: str, model: str, client: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.api_key, self.model, self.sleep = api_key, model, sleep
        self.client = client or httpx.Client(timeout=TIMEOUT_S)
        self.rules = RulesClassifier()
        self.fallbacks = 0
        self.last_was_fallback = False
        self.usage = {"in": 0, "out": 0}  # tokens, for the benchmark's cost estimate

    def _ask(self, text: str) -> str:
        raise NotImplementedError

    def complete(self, prompt: str) -> str:
        """Free text (used by the brief agent). Raises LLMError on failure."""
        raise NotImplementedError

    def classify(self, text: str) -> Classification:
        self.last_was_fallback = False
        if not text.strip():
            return Classification("other", frozenset())
        try:
            return validate(json.loads(self._ask(text)))
        except (LLMError, ValueError, KeyError, IndexError, TypeError) as exc:
            self.fallbacks += 1
            self.last_was_fallback = True
            log.warning("llm fallback to rules", extra={"provider": self.name,
                                                         "reason": str(exc)[:200]})
            return self.rules.classify(text)


class GeminiClassifier(_LLMClassifier):
    name = "gemini"
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def _ask(self, text: str) -> str:
        return self._generate(PROMPT + text, {"responseMimeType": "application/json",
                                              "temperature": 0})

    def complete(self, prompt: str) -> str:
        return self._generate(prompt, {"temperature": 0.4})

    def _generate(self, prompt: str, config: dict) -> str:
        data = post_json_with_retry(
            self.client, self.URL.format(model=self.model),
            headers={"x-goog-api-key": self.api_key},
            json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": config},
            sleep=self.sleep)
        meta = data.get("usageMetadata", {})
        self.usage["in"] += meta.get("promptTokenCount", 0)
        # thinking models bill their reasoning tokens as output too
        self.usage["out"] += (meta.get("candidatesTokenCount", 0)
                              + meta.get("thoughtsTokenCount", 0))
        return data["candidates"][0]["content"]["parts"][0]["text"]


class GroqClassifier(_LLMClassifier):
    name = "groq"
    URL = "https://api.groq.com/openai/v1/chat/completions"

    def _ask(self, text: str) -> str:
        return self._chat(PROMPT + text, {"temperature": 0,
                                          "response_format": {"type": "json_object"}})

    def complete(self, prompt: str) -> str:
        return self._chat(prompt, {"temperature": 0.4})

    def chat_tools(self, messages: list[dict], tools: list[dict]) -> dict:
        """One assistant turn with tool calling (used by the research agent)."""
        data = post_json_with_retry(
            self.client, self.URL, headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "temperature": 0.2, "messages": messages,
                  "tools": tools, "tool_choice": "required" if len(tools) == 1 else "auto"},
            sleep=self.sleep)
        return data["choices"][0]["message"]

    def _chat(self, prompt: str, options: dict) -> str:
        data = post_json_with_retry(
            self.client, self.URL, headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, **options,
                  "messages": [{"role": "user", "content": prompt}]},
            sleep=self.sleep)
        usage = data.get("usage", {})
        self.usage["in"] += usage.get("prompt_tokens", 0)
        self.usage["out"] += usage.get("completion_tokens", 0)
        return data["choices"][0]["message"]["content"]


def make_llm_classifier(settings: Settings) -> _LLMClassifier:
    if settings.llm_provider == "gemini":
        if not settings.gemini_api_key:
            raise ValueError("LLM_PROVIDER=gemini but GEMINI_API_KEY is not set")
        return GeminiClassifier(settings.gemini_api_key, settings.gemini_model)
    if settings.llm_provider == "groq":
        if not settings.groq_api_key:
            raise ValueError("LLM_PROVIDER=groq but GROQ_API_KEY is not set")
        return GroqClassifier(settings.groq_api_key, settings.groq_model)
    raise ValueError(f"unknown LLM_PROVIDER {settings.llm_provider!r}")


if __name__ == "__main__":  # python -m app.llm "some description"
    import sys
    from dataclasses import replace

    from app.config import get_settings

    for provider in ("gemini", "groq"):
        try:
            clf = make_llm_classifier(replace(get_settings(), llm_provider=provider))
        except ValueError as exc:
            print(f"{provider}: skipped ({exc})")
            continue
        print(provider, clf.classify(sys.argv[1]).to_json(), "fallback:", clf.last_was_fallback)
