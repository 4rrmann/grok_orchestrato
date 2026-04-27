"""
Key Manager — the stateful "memory" of the orchestration system.

If the Orchestrator is the decision-maker ("which key should I use?"),
the Key Manager is the information system it consults ("here's what we
know about each key, and here's how to update that knowledge").

The Key Manager owns all DB interactions for the api_keys table. It
provides two categories of operations:

  1. READ: fetching keys eligible for use, sorted by score
  2. WRITE: updating key state after a request (success, failure, cooldown)

Scoring explained
─────────────────
We want to pick the "best" key, but "best" is multidimensional:
  - Lowest fail count (reliability)
  - Lowest latency (speed)
  - Least recently used (fairness — spread load across keys)
  - Highest priority (manual tier control)

We normalise each dimension to [0, 1] relative to the current candidate
pool, then compute a weighted sum. Lower final score = better key.

Why normalise? Because fail_count (range: 0–FAILURE_THRESHOLD) and
latency (range: 0–potentially thousands of ms) are on completely different
scales. Without normalisation, a key with latency=500 would dominate
a key with fail_count=5 even if latency is less important to us.

EWMA latency explained
──────────────────────
Instead of a simple average (sum / count), we use an Exponential
Weighted Moving Average:

    new_avg = alpha * new_sample + (1 - alpha) * old_avg

With alpha=0.2, a new latency sample contributes 20% to the new average.
This means:
  - Recent measurements are weighted more than old ones
  - A single outlier (one slow request) doesn't permanently penalise a key
  - A key that becomes consistently slow degrades its score over time
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import KeyValidationError, NoAvailableKeyError
from app.core.logging_config import get_logger
from app.db.base import utcnow
from app.model.api_key import APIKey, KeyStatus
from app.schemas.api_key import APIKeyCreate, APIKeyUpdate

log = get_logger(__name__)

# Lock to prevent a "thundering herd" when multiple concurrent requests
# simultaneously find no available key and all try to update state.
_state_lock = asyncio.Lock()


class KeyManager:
    """
    Manages the lifecycle and state of all API keys in the database.

    Designed to be used as a dependency-injected service in the Orchestrator.
    All methods are async to be compatible with SQLAlchemy's async engine.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ── Selection ─────────────────────────────────────────────────────────────

    async def get_best_available_key(self) -> APIKey:
        """
        Return the single best key for the next request.

        "Best" is determined by the scoring algorithm — see module docstring.
        If no key is eligible, raises NoAvailableKeyError which the
        orchestrator translates to a 503 response.
        """
        candidates = await self._fetch_eligible_keys()

        if not candidates:
            log.warning("no_available_keys", total_in_db=await self._count_all_keys())
            raise NoAvailableKeyError(
                "No API keys are currently available. "
                "All keys may be in cooldown or disabled."
            )

        scored = self._score_keys(candidates)
        best = min(scored, key=lambda x: x[1])  # lowest score wins

        log.info(
            "key_selected",
            key_id=best[0].id,
            alias=best[0].alias,
            score=round(best[1], 4),
            candidates=len(candidates),
        )
        return best[0]

    async def _fetch_eligible_keys(self) -> list[APIKey]:
        """
        Query the DB for keys that are right now eligible to receive a request.

        Eligible means:
          - is_enabled = True (not manually disabled)
          - status = 'active' OR status = 'rate_limited' but cooldown has expired
        """
        now = utcnow()

        stmt = (
            select(APIKey)
            .where(APIKey.is_enabled == True)  # noqa: E712
            .where(
                # Either active, or rate_limited but cooldown has expired
                (APIKey.status == KeyStatus.ACTIVE)
                | (
                    (APIKey.status == KeyStatus.RATE_LIMITED)
                    & (
                        (APIKey.cooldown_until == None)  # noqa: E711
                        | (APIKey.cooldown_until <= now)
                    )
                )
            )
            .order_by(APIKey.fail_count.asc(), APIKey.avg_latency_ms.asc())
        )

        result = await self.db.execute(stmt)
        keys = list(result.scalars().all())

        # If a rate-limited key's cooldown has expired, promote it back to active.
        # We do this lazily (on selection) rather than with a background job
        # to keep the system simple — a background reactivation job is an
        # optimisation for future scaling.
        recovered = []
        for key in keys:
            if key.status == KeyStatus.RATE_LIMITED and (
                key.cooldown_until is None or key.cooldown_until <= now
            ):
                key.status = KeyStatus.ACTIVE
                key.cooldown_until = None
                recovered.append(key.alias)

        if recovered:
            await self.db.flush()
            log.info("keys_recovered_from_cooldown", aliases=recovered)

        return keys

    @staticmethod
    def _as_utc(dt: datetime) -> datetime:
        """
        Ensure a datetime is UTC-aware.

        SQLite stores datetimes without timezone info. When SQLAlchemy reads
        them back they are timezone-naive (no tzinfo). utcnow() returns
        timezone-aware. Python refuses to subtract naive from aware, so we
        normalise: if a datetime has no tzinfo, assume it is UTC and attach it.
        """
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    def _score_keys(self, keys: list[APIKey]) -> list[tuple[APIKey, float]]:
        """
        Score each key. Lower score = better candidate.

        We first extract the raw values for each dimension, find the
        min/max across the candidate pool, then normalise each to [0, 1].
        This ensures no single dimension dominates due to scale differences.
        """
        if len(keys) == 1:
            return [(keys[0], 0.0)]

        now = utcnow()  # always timezone-aware

        # Raw values for each key
        fail_counts = [k.fail_count for k in keys]
        latencies = [k.avg_latency_ms for k in keys]
        # last_used: None means never used — most desirable (no recency penalty).
        # _as_utc() normalises SQLite's naive datetimes so subtraction works.
        recency = [
            (now - self._as_utc(k.last_used)).total_seconds() if k.last_used else float("inf")
            for k in keys
        ]

        def normalise(values: list[float]) -> list[float]:
            """Normalise a list to [0, 1]. If all equal, return zeros."""
            min_v, max_v = min(values), max(values)
            if max_v == min_v:
                return [0.0] * len(values)
            return [(v - min_v) / (max_v - min_v) for v in values]

        norm_fail = normalise(fail_counts)
        norm_lat = normalise(latencies)
        # For recency, longer time since last use = lower score (more fair to use it)
        # So we invert: high recency_seconds → low penalty.
        max_recency = max(r for r in recency if r != float("inf")) if any(r != float("inf") for r in recency) else 1
        norm_recency = [
            0.0 if r == float("inf") else 1.0 - (r / max_recency)
            for r in recency
        ]

        cfg = settings
        scored = []
        for i, key in enumerate(keys):
            # Priority is an inverse bonus — higher priority = lower score
            priority_bonus = (key.priority / 100.0) * 0.5

            score = (
                cfg.SCORE_WEIGHT_FAIL_COUNT * norm_fail[i]
                + cfg.SCORE_WEIGHT_LATENCY * norm_lat[i]
                + cfg.SCORE_WEIGHT_LAST_USED * norm_recency[i]
                - priority_bonus
            )
            scored.append((key, score))

        return scored

    # ── State Updates ─────────────────────────────────────────────────────────

    async def record_success(self, key: APIKey, latency_ms: float) -> None:
        """
        Update key state after a successful API call.

        On success:
        - Reset fail_count to 0 (key is healthy again)
        - Update avg_latency_ms using EWMA
        - Record last_used timestamp
        - Increment total_requests
        - If key was rate_limited and now succeeded, restore to active
        """
        alpha = settings.LATENCY_EWMA_ALPHA

        # EWMA: weight current sample vs historical average
        if key.avg_latency_ms == 0.0:
            new_latency = latency_ms  # first data point — no history to blend
        else:
            new_latency = alpha * latency_ms + (1 - alpha) * key.avg_latency_ms

        async with _state_lock:
            await self.db.execute(
                update(APIKey)
                .where(APIKey.id == key.id)
                .values(
                    fail_count=0,
                    status=KeyStatus.ACTIVE,
                    cooldown_until=None,
                    avg_latency_ms=new_latency,
                    last_used=utcnow(),
                    total_requests=APIKey.total_requests + 1,
                )
            )
            await self.db.flush()

        log.info(
            "key_success_recorded",
            key_id=key.id,
            alias=key.alias,
            latency_ms=round(latency_ms, 2),
            new_avg_latency=round(new_latency, 2),
        )

    async def record_rate_limit(self, key: APIKey) -> None:
        """
        Handle a 429 response: put the key into cooldown.

        The key is still technically functional — it just needs to rest.
        We don't increment fail_count because rate-limiting is expected
        behaviour, not a sign of key degradation.
        """
        cooldown_until = utcnow() + timedelta(seconds=settings.COOLDOWN_SECONDS)

        async with _state_lock:
            await self.db.execute(
                update(APIKey)
                .where(APIKey.id == key.id)
                .values(
                    status=KeyStatus.RATE_LIMITED,
                    cooldown_until=cooldown_until,
                    last_used=utcnow(),
                    total_requests=APIKey.total_requests + 1,
                    total_failures=APIKey.total_failures + 1,
                )
            )
            await self.db.flush()

        log.warning(
            "key_rate_limited",
            key_id=key.id,
            alias=key.alias,
            cooldown_until=cooldown_until.isoformat(),
            cooldown_seconds=settings.COOLDOWN_SECONDS,
        )

    async def record_failure(self, key: APIKey) -> None:
        """
        Handle a transient failure (timeout, 5xx): increment fail_count.

        If fail_count reaches FAILURE_THRESHOLD, disable the key permanently.
        A disabled key will never be selected again until an operator
        re-enables it via the admin API — this is intentional. We don't
        auto-recover from repeated failures without human review.
        """
        new_fail_count = key.fail_count + 1
        should_disable = new_fail_count >= settings.FAILURE_THRESHOLD

        new_status = KeyStatus.DISABLED if should_disable else KeyStatus.ACTIVE

        async with _state_lock:
            await self.db.execute(
                update(APIKey)
                .where(APIKey.id == key.id)
                .values(
                    fail_count=new_fail_count,
                    status=new_status,
                    last_used=utcnow(),
                    total_requests=APIKey.total_requests + 1,
                    total_failures=APIKey.total_failures + 1,
                )
            )
            await self.db.flush()

        if should_disable:
            log.error(
                "key_disabled_exceeded_threshold",
                key_id=key.id,
                alias=key.alias,
                fail_count=new_fail_count,
                threshold=settings.FAILURE_THRESHOLD,
            )
        else:
            log.warning(
                "key_failure_recorded",
                key_id=key.id,
                alias=key.alias,
                fail_count=new_fail_count,
                threshold=settings.FAILURE_THRESHOLD,
            )

    async def record_auth_failure(self, key: APIKey) -> None:
        """
        Handle a 401/403: permanently disable the key immediately.

        An authentication failure means the key itself is invalid — there
        is no point retrying or waiting. Disabling immediately prevents
        any future request from wasting time on a known-bad key.
        """
        async with _state_lock:
            await self.db.execute(
                update(APIKey)
                .where(APIKey.id == key.id)
                .values(
                    status=KeyStatus.DISABLED,
                    is_enabled=False,
                    total_failures=APIKey.total_failures + 1,
                )
            )
            await self.db.flush()

        log.error(
            "key_disabled_auth_failure",
            key_id=key.id,
            alias=key.alias,
            reason="Authentication rejected by Grok API",
        )

    # ── CRUD ──────────────────────────────────────────────────────────────────

    async def create_key(self, data: APIKeyCreate) -> APIKey:
        """Register a new API key in the database."""
        # Check for duplicates before inserting
        existing = await self.db.execute(
            select(APIKey).where(APIKey.api_key == data.api_key)
        )
        if existing.scalar_one_or_none():
            raise KeyValidationError("This API key is already registered.")

        key = APIKey(
            api_key=data.api_key,
            alias=data.alias,
            priority=data.priority,
            notes=data.notes,
        )
        self.db.add(key)
        await self.db.flush()  # flush to get the auto-generated id

        log.info("key_created", key_id=key.id, alias=key.alias)
        return key

    async def update_key(self, key_id: int, data: APIKeyUpdate) -> Optional[APIKey]:
        """Partial update (PATCH) of a key's administrative fields."""
        key = await self._get_by_id(key_id)
        if not key:
            return None

        update_values = data.model_dump(exclude_none=True)
        if not update_values:
            return key

        await self.db.execute(
            update(APIKey).where(APIKey.id == key_id).values(**update_values)
        )
        await self.db.refresh(key)

        log.info("key_updated", key_id=key_id, fields=list(update_values.keys()))
        return key

    async def delete_key(self, key_id: int) -> Optional[str]:
        """
        Soft-delete a key by disabling it. Returns the key alias on success,
        None if the key doesn't exist.

        The row is kept in the database for audit history — it just stops
        appearing in the default list (include_disabled=False) and will never
        be selected for a request. Use re_enable_key() to bring it back, or
        create a new key with the same credentials.
        """
        key = await self._get_by_id(key_id)
        if not key:
            return None

        await self.db.execute(
            update(APIKey)
            .where(APIKey.id == key_id)
            .values(is_enabled=False, status=KeyStatus.DISABLED)
        )
        log.info("key_deleted", key_id=key_id, alias=key.alias)
        return key.alias

    async def re_enable_key(self, key_id: int) -> Optional[APIKey]:
        """
        Re-enable a previously soft-deleted or auto-disabled key.
        Resets fail_count to 0 and status to active so it is immediately
        eligible for selection again.
        """
        key = await self._get_by_id(key_id)
        if not key:
            return None

        await self.db.execute(
            update(APIKey)
            .where(APIKey.id == key_id)
            .values(
                is_enabled=True,
                status=KeyStatus.ACTIVE,
                fail_count=0,
                cooldown_until=None,
            )
        )
        await self.db.refresh(key)
        log.info("key_re_enabled", key_id=key_id, alias=key.alias)
        return key

    async def list_keys(
        self,
        skip: int = 0,
        limit: int = 50,
        include_disabled: bool = False,
        status_filter: Optional[KeyStatus] = None,
    ) -> tuple[list[APIKey], int]:
        """
        Return paginated keys and total count.

        By default (include_disabled=False) soft-deleted keys are hidden — this is
        why after DELETE a key disappears from the list even though the DB row is
        kept for audit history.

        include_disabled=True → show every row including soft-deleted ones.
        status_filter          → narrow to active / rate_limited / disabled.
        """
        base = select(APIKey)
        count_base = select(func.count(APIKey.id))

        if not include_disabled:
            base = base.where(APIKey.is_enabled == True)       # noqa: E712
            count_base = count_base.where(APIKey.is_enabled == True)  # noqa: E712

        if status_filter:
            base = base.where(APIKey.status == status_filter)
            count_base = count_base.where(APIKey.status == status_filter)

        count_result = await self.db.execute(count_base)
        total = count_result.scalar_one()

        result = await self.db.execute(
            base.order_by(APIKey.priority.desc(), APIKey.id).offset(skip).limit(limit)
        )
        return list(result.scalars().all()), total

    async def get_fleet_stats(self) -> dict:
        """
        Aggregate stats across all keys — for the health dashboard.

        We use SQLAlchemy's case() instead of boolean.cast(int) because
        .cast(int) passes Python's built-in int type, which SQLAlchemy 2.x
        cannot introspect (it needs its own Integer type object).
        case() is also more explicit and portable across DB backends.
        """
        result = await self.db.execute(
            select(
                func.count(APIKey.id).label("total"),
                func.sum(
                    case((APIKey.status == KeyStatus.ACTIVE, 1), else_=0)
                ).label("active"),
                func.sum(
                    case((APIKey.status == KeyStatus.RATE_LIMITED, 1), else_=0)
                ).label("rate_limited"),
                func.sum(
                    case((APIKey.status == KeyStatus.DISABLED, 1), else_=0)
                ).label("disabled"),
                func.coalesce(func.sum(APIKey.total_requests), 0).label("total_requests"),
                func.coalesce(func.sum(APIKey.total_failures), 0).label("total_failures"),
                func.avg(APIKey.avg_latency_ms).label("avg_latency"),
            )
        )
        row = result.one()
        return {
            "total_keys": row.total or 0,
            "active_keys": row.active or 0,
            "rate_limited_keys": row.rate_limited or 0,
            "disabled_keys": row.disabled or 0,
            "total_requests_lifetime": row.total_requests or 0,
            "total_failures_lifetime": row.total_failures or 0,
            "avg_latency_ms_fleet": round(row.avg_latency or 0.0, 2),
        }

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _get_by_id(self, key_id: int) -> Optional[APIKey]:
        result = await self.db.execute(select(APIKey).where(APIKey.id == key_id))
        return result.scalar_one_or_none()

    async def _count_all_keys(self) -> int:
        result = await self.db.execute(select(func.count(APIKey.id)))
        return result.scalar_one()
