import os
from dataclasses import dataclass
from functools import lru_cache


def _bool(name: str) -> bool:
    return os.environ.get(name, "0").strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    admin_token: str
    webhook_secret: str
    llm_provider: str = "rules"
    crm_provider: str = "fake"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    hubspot_token: str = ""
    slack_webhook_url: str = ""
    fuzzy_threshold: int = 92  # tuned by bench.run on seed 1
    backoff_base_s: float = 2.0
    run_worker: bool = False
    demo_mode: bool = False


@lru_cache
def get_settings() -> Settings:
    env = os.environ.get
    return Settings(
        database_url=env("DATABASE_URL", "postgresql://lead:lead@localhost:5433/lead"),
        admin_token=env("ADMIN_TOKEN", ""),
        webhook_secret=env("WEBHOOK_SECRET", ""),
        llm_provider=env("LLM_PROVIDER", "rules"),
        crm_provider=env("CRM_PROVIDER", "fake"),
        gemini_api_key=env("GEMINI_API_KEY", ""),
        gemini_model=env("GEMINI_MODEL", "gemini-3.8-flash"),
        groq_api_key=env("GROQ_API_KEY", ""),
        groq_model=env("GROQ_MODEL", "openai/gpt-oss-120b"),
        hubspot_token=env("HUBSPOT_TOKEN", ""),
        slack_webhook_url=env("SLACK_WEBHOOK_URL", ""),
        fuzzy_threshold=int(env("FUZZY_THRESHOLD", "92")),
        backoff_base_s=float(env("BACKOFF_BASE_S", "2")),
        run_worker=_bool("RUN_WORKER"),
        demo_mode=_bool("DEMO_MODE"),
    )
