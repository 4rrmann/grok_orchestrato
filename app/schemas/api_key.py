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

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class KeyStatus(str, enum.Enum):
    """
    String enum so values are stored as readable strings in the DB
    ("active", not "0"), which makes raw SQL queries and log messages
    immediately understandable without a lookup table.
    """
    ACTIVE = "active"
    RATE_LIMITED = "rate_limited"
    DISABLED = "disabled"


class APIKey(Base, TimestampMixin):
    __tablename__ = "api_keys"

    # ── Identity ─────────────────────────────────────────────────────────────
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    api_key: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="The actual secret API key — never log this value",
    )

    alias: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        default="unnamed",
        comment="Human-readable label shown in logs and dashboards",
    )

    # ── State machine ─────────────────────────────────────────────────────────
    status: Mapped[KeyStatus] = mapped_column(
        String(20),
        nullable=False,
        default=KeyStatus.ACTIVE,
        index=True,   # we filter by status on every key-selection query
        comment="Current operational state of this key",
    )

    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Manual on/off switch; False overrides any status value",
    )

    # ── Failure tracking ─────────────────────────────────────────────────────
    fail_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Consecutive failure count — resets to 0 on any success",
    )

    total_requests: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Lifetime request count — never resets",
    )

    total_failures: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Lifetime failure count — never resets",
    )

    # ── Timing ───────────────────────────────────────────────────────────────
    last_used: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        comment="Timestamp of the last request dispatched with this key",
    )

    cooldown_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        comment="Key is ineligible for selection until this timestamp (rate limiting)",
    )

    # ── Performance ──────────────────────────────────────────────────────────
    avg_latency_ms: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
        comment="EWMA of response latency in milliseconds",
    )

    # ── Administrative ───────────────────────────────────────────────────────
    priority: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Higher value = preferred during key selection (manual tier control)",
    )

    notes: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        default=None,
        comment="Free-text operator notes (purchase date, plan tier, etc.)",
    )

    # ── Constraints & indexes ────────────────────────────────────────────────
    __table_args__ = (
        UniqueConstraint("api_key", name="uq_api_keys_api_key"),
        # Composite index: the key selection query filters on status + is_enabled,
        # then orders by fail_count and avg_latency. This index covers that query.
        Index("ix_api_keys_status_enabled_fail", "status", "is_enabled", "fail_count"),
    )

    def __repr__(self) -> str:
        # We deliberately show only the alias and status, NEVER the key itself.
        return (
            f"<APIKey id={self.id} alias={self.alias!r} "
            f"status={self.status} fail_count={self.fail_count}>"
        )

    @property
    def masked_key(self) -> str:
        """Returns a safe representation for logs: 'xai-abc...xyz'"""
        if len(self.api_key) <= 8:
            return "***"
        return f"{self.api_key[:7]}...{self.api_key[-4:]}"
