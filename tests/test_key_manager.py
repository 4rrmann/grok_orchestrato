"""
Tests for KeyManager — the most critical piece of business logic.

We test:
  1. Key selection: does the scoring algorithm pick the right key?
  2. State transitions: do success/failure/rate-limit updates work correctly?
  3. Cooldown: does a rate-limited key come back after cooldown expires?
  4. Disabling: does a key get disabled after FAILURE_THRESHOLD failures?
  5. CRUD: create, list, update, delete operations
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import pytest_asyncio

from app.core.exceptions import KeyValidationError, NoAvailableKeyError
from app.models.api_key import APIKey, KeyStatus
from app.schemas.api_key import APIKeyCreate, APIKeyUpdate
from app.services.key_manager import KeyManager


pytestmark = pytest.mark.asyncio


class TestKeySelection:
    """Key selection and scoring algorithm tests."""

    async def test_selects_lowest_fail_count(self, db_session, sample_keys):
        """Given two active keys, the one with fewer failures should be preferred."""
        km = KeyManager(db_session)
        selected = await km.get_best_available_key()
        # sample_keys[0] = fail_count=0, sample_keys[1] = fail_count=1
        assert selected.alias == "fast-healthy"

    async def test_raises_when_no_keys(self, db_session):
        """Empty key pool should raise NoAvailableKeyError."""
        km = KeyManager(db_session)
        with pytest.raises(NoAvailableKeyError):
            await km.get_best_available_key()

    async def test_skips_disabled_keys(self, db_session):
        """Disabled keys must never be selected."""
        key = APIKey(
            api_key="xai-disabled-key",
            alias="disabled",
            status=KeyStatus.DISABLED,
            is_enabled=False,
        )
        db_session.add(key)
        await db_session.flush()

        km = KeyManager(db_session)
        with pytest.raises(NoAvailableKeyError):
            await km.get_best_available_key()

    async def test_skips_rate_limited_key_in_cooldown(self, db_session):
        """Rate-limited key with future cooldown_until must not be selected."""
        key = APIKey(
            api_key="xai-cooling-key",
            alias="cooling",
            status=KeyStatus.RATE_LIMITED,
            is_enabled=True,
            cooldown_until=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db_session.add(key)
        await db_session.flush()

        km = KeyManager(db_session)
        with pytest.raises(NoAvailableKeyError):
            await km.get_best_available_key()

    async def test_recovers_rate_limited_key_after_cooldown(self, db_session):
        """A rate-limited key whose cooldown has EXPIRED should be selectable."""
        key = APIKey(
            api_key="xai-recovered-key",
            alias="recovered",
            status=KeyStatus.RATE_LIMITED,
            is_enabled=True,
            # cooldown expired 5 minutes ago
            cooldown_until=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        db_session.add(key)
        await db_session.flush()

        km = KeyManager(db_session)
        selected = await km.get_best_available_key()
        assert selected.alias == "recovered"
        # Should have been promoted back to active
        assert selected.status == KeyStatus.ACTIVE

    async def test_priority_key_preferred(self, db_session):
        """A high-priority key should beat a lower-priority key even with same stats."""
        low = APIKey(api_key="xai-low-prio", alias="low", status=KeyStatus.ACTIVE, priority=0)
        high = APIKey(api_key="xai-high-prio", alias="high", status=KeyStatus.ACTIVE, priority=90)
        db_session.add_all([low, high])
        await db_session.flush()

        km = KeyManager(db_session)
        selected = await km.get_best_available_key()
        assert selected.alias == "high"


class TestStateTransitions:
    """Tests for record_success, record_rate_limit, record_failure, record_auth_failure."""

    async def test_record_success_resets_fail_count(self, db_session):
        key = APIKey(
            api_key="xai-failing-key",
            alias="was-failing",
            status=KeyStatus.ACTIVE,
            fail_count=3,
        )
        db_session.add(key)
        await db_session.flush()

        km = KeyManager(db_session)
        await km.record_success(key, latency_ms=200.0)
        await db_session.refresh(key)

        assert key.fail_count == 0
        assert key.status == KeyStatus.ACTIVE
        assert key.last_used is not None

    async def test_record_success_updates_ewma_latency(self, db_session):
        """EWMA should blend new and old latency values."""
        key = APIKey(
            api_key="xai-latency-key",
            alias="latency-test",
            status=KeyStatus.ACTIVE,
            avg_latency_ms=500.0,
        )
        db_session.add(key)
        await db_session.flush()

        km = KeyManager(db_session)
        await km.record_success(key, latency_ms=100.0)
        await db_session.refresh(key)

        # EWMA: 0.2 * 100 + 0.8 * 500 = 20 + 400 = 420
        assert abs(key.avg_latency_ms - 420.0) < 1.0

    async def test_record_rate_limit_sets_cooldown(self, db_session):
        key = APIKey(api_key="xai-rl-key", alias="rl-test", status=KeyStatus.ACTIVE)
        db_session.add(key)
        await db_session.flush()

        km = KeyManager(db_session)
        await km.record_rate_limit(key)
        await db_session.refresh(key)

        assert key.status == KeyStatus.RATE_LIMITED
        assert key.cooldown_until is not None
        assert key.cooldown_until > datetime.now(timezone.utc)

    async def test_record_failure_increments_count(self, db_session):
        key = APIKey(api_key="xai-fail-key", alias="fail-test", status=KeyStatus.ACTIVE, fail_count=0)
        db_session.add(key)
        await db_session.flush()

        km = KeyManager(db_session)
        await km.record_failure(key)
        await db_session.refresh(key)

        assert key.fail_count == 1
        assert key.status == KeyStatus.ACTIVE  # not yet at threshold

    async def test_record_failure_disables_at_threshold(self, db_session):
        """Key at FAILURE_THRESHOLD - 1 should be disabled after one more failure."""
        from app.core.config import settings

        key = APIKey(
            api_key="xai-threshold-key",
            alias="threshold-test",
            status=KeyStatus.ACTIVE,
            fail_count=settings.FAILURE_THRESHOLD - 1,
        )
        db_session.add(key)
        await db_session.flush()

        km = KeyManager(db_session)
        await km.record_failure(key)
        await db_session.refresh(key)

        assert key.status == KeyStatus.DISABLED
        assert key.fail_count == settings.FAILURE_THRESHOLD

    async def test_record_auth_failure_disables_immediately(self, db_session):
        """Authentication failure must disable the key immediately, regardless of fail_count."""
        key = APIKey(api_key="xai-bad-key", alias="bad-auth", status=KeyStatus.ACTIVE, fail_count=0)
        db_session.add(key)
        await db_session.flush()

        km = KeyManager(db_session)
        await km.record_auth_failure(key)
        await db_session.refresh(key)

        assert key.status == KeyStatus.DISABLED
        assert key.is_enabled is False


class TestCRUD:
    """CRUD operation tests."""

    async def test_create_key(self, db_session):
        km = KeyManager(db_session)
        key = await km.create_key(APIKeyCreate(api_key="xai-brand-new-key", alias="new-key"))
        assert key.id is not None
        assert key.alias == "new-key"
        assert key.status == KeyStatus.ACTIVE

    async def test_create_duplicate_key_raises(self, db_session):
        km = KeyManager(db_session)
        await km.create_key(APIKeyCreate(api_key="xai-duplicate-key", alias="original"))
        with pytest.raises(KeyValidationError, match="already registered"):
            await km.create_key(APIKeyCreate(api_key="xai-duplicate-key", alias="duplicate"))

    async def test_update_key_alias(self, db_session):
        key = APIKey(api_key="xai-update-key", alias="old-alias", status=KeyStatus.ACTIVE)
        db_session.add(key)
        await db_session.flush()

        km = KeyManager(db_session)
        updated = await km.update_key(key.id, APIKeyUpdate(alias="new-alias"))
        assert updated.alias == "new-alias"

    async def test_delete_key_soft_disables(self, db_session):
        key = APIKey(api_key="xai-delete-me", alias="to-delete", status=KeyStatus.ACTIVE)
        db_session.add(key)
        await db_session.flush()

        km = KeyManager(db_session)
        result = await km.delete_key(key.id)
        assert result is True
        await db_session.refresh(key)
        assert key.is_enabled is False
        assert key.status == KeyStatus.DISABLED

    async def test_fleet_stats(self, db_session, sample_keys):
        km = KeyManager(db_session)
        stats = await km.get_fleet_stats()
        assert stats["total_keys"] == 3
        assert stats["active_keys"] == 2
        assert stats["rate_limited_keys"] == 1
