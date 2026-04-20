"""
Route integration tests — testing the HTTP layer end to end.

These tests spin up the full FastAPI app (via AsyncClient) but with:
  - In-memory SQLite instead of the real DB
  - Mocked Orchestrator instead of a real Grok API call

This gives us high confidence in the HTTP contract (correct status codes,
response shapes, headers) without depending on external services.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.core.exceptions import AllRetriesExhaustedError, NoAvailableKeyError
from app.schemas.request import AIResponse, UsageStats

pytestmark = pytest.mark.asyncio

ADMIN_HEADERS = {"X-Admin-Key": settings.ADMIN_API_KEY}


# ─────────────────────────────────────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────────────────────────────────────

class TestHealth:
    async def test_health_returns_200(self, test_client: AsyncClient):
        response = await test_client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


# ─────────────────────────────────────────────────────────────────────────────
# Key management routes
# ─────────────────────────────────────────────────────────────────────────────

class TestKeyRoutes:
    async def test_create_key_returns_201(self, test_client: AsyncClient):
        response = await test_client.post(
            "/admin/keys",
            headers=ADMIN_HEADERS,
            json={"api_key": "xai-route-test-key-001", "alias": "route-test"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["alias"] == "route-test"
        assert "api_key" not in data       # raw key must NEVER appear in response
        assert "masked_key" in data        # only the masked version

    async def test_create_key_without_admin_header_returns_401(self, test_client: AsyncClient):
        response = await test_client.post(
            "/admin/keys",
            json={"api_key": "xai-unauthorized-key"},
        )
        assert response.status_code == 422  # missing required header = 422 Unprocessable

    async def test_create_key_wrong_admin_header_returns_401(self, test_client: AsyncClient):
        response = await test_client.post(
            "/admin/keys",
            headers={"X-Admin-Key": "wrong-key"},
            json={"api_key": "xai-bad-admin"},
        )
        assert response.status_code == 401

    async def test_list_keys_empty(self, test_client: AsyncClient):
        response = await test_client.get("/admin/keys", headers=ADMIN_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["keys"] == []

    async def test_list_keys_populated(self, test_client: AsyncClient):
        # Create two keys first
        for i in range(2):
            await test_client.post(
                "/admin/keys",
                headers=ADMIN_HEADERS,
                json={"api_key": f"xai-list-test-key-{i:03d}", "alias": f"key-{i}"},
            )
        response = await test_client.get("/admin/keys", headers=ADMIN_HEADERS)
        assert response.status_code == 200
        assert response.json()["total"] == 2

    async def test_update_key(self, test_client: AsyncClient):
        # Create
        create_resp = await test_client.post(
            "/admin/keys",
            headers=ADMIN_HEADERS,
            json={"api_key": "xai-update-route-key", "alias": "original-alias"},
        )
        key_id = create_resp.json()["id"]

        # Update alias
        response = await test_client.patch(
            f"/admin/keys/{key_id}",
            headers=ADMIN_HEADERS,
            json={"alias": "updated-alias"},
        )
        assert response.status_code == 200
        assert response.json()["alias"] == "updated-alias"

    async def test_update_nonexistent_key_returns_404(self, test_client: AsyncClient):
        response = await test_client.patch(
            "/admin/keys/99999",
            headers=ADMIN_HEADERS,
            json={"alias": "ghost"},
        )
        assert response.status_code == 404

    async def test_delete_key(self, test_client: AsyncClient):
        create_resp = await test_client.post(
            "/admin/keys",
            headers=ADMIN_HEADERS,
            json={"api_key": "xai-delete-route-key", "alias": "to-delete"},
        )
        key_id = create_resp.json()["id"]

        response = await test_client.delete(f"/admin/keys/{key_id}", headers=ADMIN_HEADERS)
        assert response.status_code == 204

    async def test_fleet_stats(self, test_client: AsyncClient):
        response = await test_client.get("/admin/keys/stats", headers=ADMIN_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert "total_keys" in data
        assert "active_keys" in data

    async def test_metrics_endpoint(self, test_client: AsyncClient):
        response = await test_client.get("/admin/metrics", headers=ADMIN_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert "uptime_seconds" in data
        assert "rolling_window" in data


# ─────────────────────────────────────────────────────────────────────────────
# AI route — mocking the Orchestrator to control outcomes
# ─────────────────────────────────────────────────────────────────────────────

class TestAIRoute:
    """
    We patch Orchestrator.handle_request so we can control exactly what the
    Orchestrator returns or raises — keeping tests fast and deterministic.
    """

    def _mock_ai_response(self) -> AIResponse:
        return AIResponse(
            content="Mocked AI response",
            model="grok-3",
            usage=UsageStats(prompt_tokens=5, completion_tokens=10, total_tokens=15),
            key_alias="test-key",
            attempts=1,
            latency_ms=99.9,
        )

    async def test_successful_ai_request(self, test_client: AsyncClient):
        with patch(
            "app.api.routes.ai.Orchestrator.handle_request",
            AsyncMock(return_value=self._mock_ai_response()),
        ):
            response = await test_client.post(
                "/v1/ask-ai",
                json={"messages": [{"role": "user", "content": "Hello!"}]},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["content"] == "Mocked AI response"
        assert data["key_alias"] == "test-key"
        assert data["attempts"] == 1
        assert "X-Request-ID" in response.headers

    async def test_no_available_key_returns_503(self, test_client: AsyncClient):
        with patch(
            "app.api.routes.ai.Orchestrator.handle_request",
            AsyncMock(side_effect=NoAvailableKeyError("Pool empty")),
        ):
            response = await test_client.post(
                "/v1/ask-ai",
                json={"messages": [{"role": "user", "content": "Hello!"}]},
            )

        assert response.status_code == 503

    async def test_all_retries_exhausted_returns_502(self, test_client: AsyncClient):
        with patch(
            "app.api.routes.ai.Orchestrator.handle_request",
            AsyncMock(side_effect=AllRetriesExhaustedError(attempts=3, last_error="timeout")),
        ):
            response = await test_client.post(
                "/v1/ask-ai",
                json={"messages": [{"role": "user", "content": "Hello!"}]},
            )

        assert response.status_code == 502

    async def test_invalid_request_returns_422(self, test_client: AsyncClient):
        """Empty messages list should fail Pydantic validation → 422."""
        response = await test_client.post(
            "/v1/ask-ai",
            json={"messages": []},  # min_length=1 violated
        )
        assert response.status_code == 422

    async def test_invalid_role_returns_422(self, test_client: AsyncClient):
        """Unknown message role should fail Pydantic validation → 422."""
        response = await test_client.post(
            "/v1/ask-ai",
            json={"messages": [{"role": "robot", "content": "Hi"}]},
        )
        assert response.status_code == 422

    async def test_request_id_in_response_header(self, test_client: AsyncClient):
        """Every response must include X-Request-ID for log correlation."""
        with patch(
            "app.api.routes.ai.Orchestrator.handle_request",
            AsyncMock(return_value=self._mock_ai_response()),
        ):
            response = await test_client.post(
                "/v1/ask-ai",
                json={"messages": [{"role": "user", "content": "Test"}]},
            )

        assert "X-Request-ID" in response.headers
        assert len(response.headers["X-Request-ID"]) == 36  # UUID format

    async def test_custom_request_id_echoed_back(self, test_client: AsyncClient):
        """If the client sends X-Request-ID, the same value must be echoed back."""
        custom_id = "my-custom-id-12345"
        with patch(
            "app.api.routes.ai.Orchestrator.handle_request",
            AsyncMock(return_value=self._mock_ai_response()),
        ):
            response = await test_client.post(
                "/v1/ask-ai",
                headers={"X-Request-ID": custom_id},
                json={"messages": [{"role": "user", "content": "Test"}]},
            )

        assert response.headers["X-Request-ID"] == custom_id
