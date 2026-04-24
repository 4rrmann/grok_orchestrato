"""
APIKey model — the central data structure of the entire system.

Each row represents one Grok API key and its full operational state.
Think of this table as a "health dashboard" for your key fleet — every
field tells the orchestrator something important about whether this key
should be used, and how much to trust it.

Field-by-field reasoning
─────────────────────────
id              → surrogate primary key; never expose the real key as an ID
api_key         → the actual secret; indexed for fast lookup but never logged
alias           → human-readable label ("prod-key-1") so logs are readable
status          → a state machine: active → rate_limited → disabled
                  active:       usable right now
                  rate_limited: temporarily cooling down (cooldown_until applies)
                  disabled:     permanently taken out of rotation (too many failures
                                or manual override)
fail_count      → consecutive failure counter; resets to 0 on any success.
                  Consecutive (not total) because a key that fails once then
                  succeeds is healthy again.
last_used       → timestamp of last request; used to enforce fairness — if two
                  keys have identical scores, prefer the one used least recently.
cooldown_until  → when a rate-limited key is eligible to come back. The orchestrator
                  compares this against utcnow() on every selection pass.
avg_latency_ms  → exponential moving average (EWMA) of response times.
                  EWMA is better than a simple average because it weights recent
                  observations more heavily — a key that was fast yesterday but
                  is slow today should reflect today's reality quickly.
priority        → manual override weight; useful when you have keys with different
                  quota tiers (e.g., a "premium" key you want to prefer).
total_requests  → lifetime counter; never resets; useful for auditing and
                  understanding overall key utilisation.
total_failures  → lifetime failure counter; use to identify chronically bad keys.
notes           → free-text field for operators ("purchased 2024-01", "enterprise tier")
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, field_validator, ConfigDict

from app.model.api_key import KeyStatus


class APIKeyCreate(BaseModel):
    api_key: str = Field(..., min_length=10)
    alias: str = Field(default="unnamed", max_length=100)
    priority: int = Field(default=0, ge=0, le=100)
    notes: Optional[str] = Field(default=None, max_length=1000)

    @field_validator("api_key")
    @classmethod
    def key_must_not_be_whitespace(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("api_key cannot be blank or whitespace")
        return v.strip()


class APIKeyUpdate(BaseModel):
    alias: Optional[str] = Field(default=None, max_length=100)
    status: Optional[KeyStatus] = None
    is_enabled: Optional[bool] = None
    priority: Optional[int] = Field(default=None, ge=0, le=100)
    notes: Optional[str] = Field(default=None, max_length=1000)


class APIKeyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    alias: str
    status: KeyStatus
    is_enabled: bool
    fail_count: int
    total_requests: int
    total_failures: int
    avg_latency_ms: float
    priority: int
    last_used: Optional[datetime]
    cooldown_until: Optional[datetime]
    notes: Optional[str]
    created_at: datetime
    updated_at: datetime
    masked_key: str


class APIKeyList(BaseModel):
    total: int
    keys: list[APIKeyRead]


class APIKeyStats(BaseModel):
    total_keys: int
    active_keys: int
    rate_limited_keys: int
    disabled_keys: int
    total_requests_lifetime: int
    total_failures_lifetime: int
    avg_latency_ms_fleet: float
