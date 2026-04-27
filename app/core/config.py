"""
Core configuration — the single source of truth for all settings.

We use Pydantic's BaseSettings so every value can be overridden via
environment variables, making this 12-factor-app compliant. The .env
file is a convenience for local dev; in production, inject env vars
directly into the container runtime.
"""

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, AnyHttpUrl


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Application ──────────────────────────────────────────────────────────
    APP_NAME: str = "Grok API Orchestrator"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # ── Database ─────────────────────────────────────────────────────────────
    # SQLite for dev, PostgreSQL for prod:
    # "postgresql+asyncpg://user:pass@host:5432/dbname"
    DATABASE_URL: str = "sqlite+aiosqlite:///./grok_orchestrator.db"

    # ── Grok API ─────────────────────────────────────────────────────────────
    GROK_BASE_URL: str = "https://api.groq.com/openai/v1"
    GROK_DEFAULT_MODEL: str = "llama-3.3-70b-versatile"
    GROK_REQUEST_TIMEOUT: float = 30.0    # seconds before we treat it as timeout

    # ── Orchestration ────────────────────────────────────────────────────────
    MAX_RETRIES: int = 3                  # how many different keys to try before giving up
    COOLDOWN_SECONDS: int = 60            # how long a rate-limited key sits out
    FAILURE_THRESHOLD: int = 5            # consecutive fails before a key is disabled
    LATENCY_EWMA_ALPHA: float = 0.2       # exponential moving average smoothing factor
                                          # 0.2 = "remember the past heavily"

    # ── Key Selection Scoring ────────────────────────────────────────────────
    # Weights that determine how we score each key when choosing one.
    # Adjust these to tune the load-balancing behaviour.
    SCORE_WEIGHT_FAIL_COUNT: float = 0.4
    SCORE_WEIGHT_LATENCY: float = 0.4
    SCORE_WEIGHT_LAST_USED: float = 0.2   # recency penalty to ensure fairness

    # ── Security ─────────────────────────────────────────────────────────────
    API_SECRET_KEY: str = "change-me-in-production"
    ADMIN_API_KEY: str = "admin-secret-change-me"   # protects key-management endpoints


@lru_cache()  # Called once, then cached — avoids re-parsing env on every request
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
