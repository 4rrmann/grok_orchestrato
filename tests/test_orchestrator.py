"""
Tests for the Orchestrator — the retry and failure routing loop.

We test:
  1. Happy path: single attempt succeeds
  2. Rate limit: first key rate-limited, second succeeds
  3. Auth failure: bad key is disabled, fallback key succeeds
  4. Timeout: transient failure triggers fallback
  5. All retries exhausted: all keys fail → AllRetriesExhaustedError
  6. No keys: pool empty → NoAvailableKeyError (not swallowed by retry loop)
  7. Client error (4xx bad payload): does NOT retry
  8. Response enrichment: alias, attempts, latency present in response
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from app.core.exceptions import (
    AllRetriesExhaustedError,
    AuthenticationError,
    GrokClientError,
    GrokTimeoutError,
    NoAvailableKeyError,
    RateLimitError,
)
from app.models.api_key import APIKey, KeyStatus
from app.schemas.request import AIRequest, Message
from app.services.orchestrator import Orchestrator
from tests.conftest import make_mock_grok_response

pytestmark = pytest.mark.asyncio


def make_request(max_retries: int = 3) -> AIRequest:
    return AIRequest(
        messages=[Message(role="user", content="hello")],
        max_retries=max_retries,
    )


def make_key(key_id: int = 1, alias: str = "test-key") -> APIKey:
    k = APIKey(api_key=f"xai-key-{key_id}", alias=alias)
    k.id = key_id
    k.status = KeyStatus.ACTIVE
    k.fail_count = 0
    k.avg_latency_ms = 100.0
    return k


class TestHappyPath:
    async def test_single_attempt_success(self, db_session, mock_grok_success):
        """First key succeeds — response is returned with attempt count = 1."""
        orchestrator = Orchestrator(db=db_session, client=mock_grok_success)

        key = make_key()
        with (
            patch.object(orchestrator.key_manager, "get_best_available_key", AsyncMock(return_value=key)),
            patch.object(orchestrator.key_manager, "record_success", AsyncMock()),
        ):
            response = await orchestrator.handle_request(make_request(), request_id="test-001")

        assert response.content == "Hello from Grok!"
        assert response.attempts == 1
        assert response.key_alias == "test-key"
        assert response.latency_ms > 0

    async def test_response_includes_usage_stats(self, db_session, mock_grok_success):
        orchestrator = Orchestrator(db=db_session, client=mock_grok_success)
        key = make_key()
        with (
            patch.object(orchestrator.key_manager, "get_best_available_key", AsyncMock(return_value=key)),
            patch.object(orchestrator.key_manager, "record_success", AsyncMock()),
        ):
            response = await orchestrator.handle_request(make_request(), request_id="test-002")

        assert response.usage.prompt_tokens == 10
        assert response.usage.completion_tokens == 20
        assert response.usage.total_tokens == 30


class TestRetryBehaviour:
    async def test_rate_limited_key_falls_back(self, db_session):
        """
        First key returns 429. Orchestrator should:
          1. Call record_rate_limit on key 1
          2. Select key 2
          3. Succeed with key 2
          4. Return attempts=2
        """
        key1 = make_key(key_id=1, alias="rate-limited-key")
        key2 = make_key(key_id=2, alias="fallback-key")

        mock_client = MagicMock()
        mock_client.complete = AsyncMock(
            side_effect=[
                RateLimitError("429", status_code=429, key_id=1),
                make_mock_grok_response(content="Fallback success"),
            ]
        )

        orchestrator = Orchestrator(db=db_session, client=mock_client)

        select_calls = [key1, key2, key2]  # first=key1, then key2 (returned again to pass the exclude check)
        # Simpler: make get_best_available_key return key1 first, key2 thereafter
        select_mock = AsyncMock(side_effect=[key1, key2, key2, key2])
        record_rate_limit = AsyncMock()
        record_success = AsyncMock()

        with (
            patch.object(orchestrator.key_manager, "get_best_available_key", select_mock),
            patch.object(orchestrator.key_manager, "record_rate_limit", record_rate_limit),
            patch.object(orchestrator.key_manager, "record_success", record_success),
        ):
            response = await orchestrator.handle_request(make_request(max_retries=3), request_id="test-003")

        assert response.content == "Fallback success"
        assert response.attempts == 2
        assert response.key_alias == "fallback-key"
        record_rate_limit.assert_awaited_once_with(key1)
        record_success.assert_awaited_once()

    async def test_auth_failure_disables_and_falls_back(self, db_session):
        """Authentication failure on key 1 should disable it and try key 2."""
        key1 = make_key(key_id=1, alias="bad-key")
        key2 = make_key(key_id=2, alias="good-key")

        mock_client = MagicMock()
        mock_client.complete = AsyncMock(
            side_effect=[
                AuthenticationError("401", status_code=401, key_id=1),
                make_mock_grok_response(),
            ]
        )

        orchestrator = Orchestrator(db=db_session, client=mock_client)
        record_auth = AsyncMock()
        record_success = AsyncMock()

        with (
            patch.object(orchestrator.key_manager, "get_best_available_key", AsyncMock(side_effect=[key1, key2, key2])),
            patch.object(orchestrator.key_manager, "record_auth_failure", record_auth),
            patch.object(orchestrator.key_manager, "record_success", record_success),
        ):
            response = await orchestrator.handle_request(make_request(), request_id="test-004")

        assert response.attempts == 2
        record_auth.assert_awaited_once_with(key1)

    async def test_timeout_increments_fail_count(self, db_session):
        """Timeout on key 1 should call record_failure and try key 2."""
        key1 = make_key(key_id=1, alias="slow-key")
        key2 = make_key(key_id=2, alias="fast-key")

        mock_client = MagicMock()
        mock_client.complete = AsyncMock(
            side_effect=[
                GrokTimeoutError("Timed out", key_id=1),
                make_mock_grok_response(),
            ]
        )

        orchestrator = Orchestrator(db=db_session, client=mock_client)
        record_failure = AsyncMock()
        record_success = AsyncMock()

        with (
            patch.object(orchestrator.key_manager, "get_best_available_key", AsyncMock(side_effect=[key1, key2, key2])),
            patch.object(orchestrator.key_manager, "record_failure", record_failure),
            patch.object(orchestrator.key_manager, "record_success", record_success),
        ):
            response = await orchestrator.handle_request(make_request(), request_id="test-005")

        assert response.attempts == 2
        record_failure.assert_awaited_once_with(key1)


class TestExhaustionAndEdgeCases:
    async def test_all_retries_exhausted_raises(self, db_session, mock_grok_timeout):
        """If every key times out, AllRetriesExhaustedError should be raised."""
        keys = [make_key(key_id=i, alias=f"key-{i}") for i in range(1, 4)]
        orchestrator = Orchestrator(db=db_session, client=mock_grok_timeout)

        with (
            patch.object(orchestrator.key_manager, "get_best_available_key", AsyncMock(side_effect=keys * 10)),
            patch.object(orchestrator.key_manager, "record_failure", AsyncMock()),
        ):
            with pytest.raises(AllRetriesExhaustedError) as exc_info:
                await orchestrator.handle_request(make_request(max_retries=3), request_id="test-006")

        assert exc_info.value.attempts == 3

    async def test_no_available_key_propagates(self, db_session):
        """NoAvailableKeyError should propagate out — it's not a retry-able condition."""
        mock_client = MagicMock()
        orchestrator = Orchestrator(db=db_session, client=mock_client)

        with patch.object(
            orchestrator.key_manager,
            "get_best_available_key",
            AsyncMock(side_effect=NoAvailableKeyError("Pool empty")),
        ):
            with pytest.raises(NoAvailableKeyError):
                await orchestrator.handle_request(make_request(), request_id="test-007")

    async def test_client_error_not_retried(self, db_session):
        """GrokClientError (bad payload) must NOT be retried — raise immediately."""
        key = make_key()
        mock_client = MagicMock()
        mock_client.complete = AsyncMock(
            side_effect=GrokClientError("400 bad request", status_code=400, key_id=1)
        )

        orchestrator = Orchestrator(db=db_session, client=mock_client)

        with (
            patch.object(orchestrator.key_manager, "get_best_available_key", AsyncMock(return_value=key)),
        ):
            with pytest.raises(GrokClientError):
                await orchestrator.handle_request(make_request(max_retries=3), request_id="test-008")

        # Should have only called complete once — no retry
        assert mock_client.complete.await_count == 1
