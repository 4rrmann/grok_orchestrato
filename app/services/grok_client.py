"""
Grok Client — the ONLY place that talks to the Grok HTTP API.

This module has a single, narrow responsibility: take a key and a request,
make the HTTP call, return the result, or raise a typed exception.

It contains NO business logic — no retry decisions, no key selection,
no state updates. Those concerns belong to the Orchestrator. This clear
boundary means you can swap out Grok for any other provider by only
changing this file — no other layer needs to know.

Why httpx instead of requests?
  `httpx` has a native async client that plays well with FastAPI's event loop.
  `requests` is synchronous — every API call would block the entire server.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.core.config import settings
from app.core.exceptions import (
    AuthenticationError,
    GrokAPIError,
    GrokClientError,
    GrokServerError,
    GrokTimeoutError,
    RateLimitError,
)
from app.core.logging_config import get_logger
from app.schemas.request import AIRequest

log = get_logger(__name__)


class GrokResponse:
    """
    A structured wrapper around the raw Grok API response.

    We parse what we need into typed fields and store the full raw dict
    for debugging. This prevents callers from having to know the exact
    Grok response shape — if Grok changes their JSON structure, only
    this class needs to change.
    """

    __slots__ = (
        "content", "model", "finish_reason", "response_id",
        "prompt_tokens", "completion_tokens", "total_tokens",
        "latency_ms", "raw",
    )

    def __init__(self, raw: dict[str, Any], latency_ms: float) -> None:
        self.raw = raw
        self.latency_ms = latency_ms

        # Navigate Grok's response structure safely.
        choices = raw.get("choices", [])
        first_choice = choices[0] if choices else {}
        message = first_choice.get("message", {})

        self.content: str = message.get("content", "")
        self.model: str = raw.get("model", settings.GROK_DEFAULT_MODEL)
        self.finish_reason: str = first_choice.get("finish_reason", "")
        self.response_id: str = raw.get("id", "")

        usage = raw.get("usage", {})
        self.prompt_tokens: int = usage.get("prompt_tokens", 0)
        self.completion_tokens: int = usage.get("completion_tokens", 0)
        self.total_tokens: int = usage.get("total_tokens", 0)


class GrokClient:
    """
    Thin async wrapper around the Grok REST API.

    We instantiate a single shared `httpx.AsyncClient` per application
    lifetime (created in __init__, reused across calls). Connection pooling
    makes this vastly more efficient than creating a new client per request —
    TCP handshakes and TLS negotiation are expensive.
    """

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=str(settings.GROK_BASE_URL),
            timeout=httpx.Timeout(
                connect=5.0,          # time to establish TCP connection
                read=settings.GROK_REQUEST_TIMEOUT,  # time to receive response body
                write=5.0,            # time to send request body
                pool=2.0,             # time to acquire a connection from the pool
            ),
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"{settings.APP_NAME}/{settings.APP_VERSION}",
            },
            # Follow redirects automatically — Grok may issue 301s in future
            follow_redirects=True,
        )

    async def complete(
        self,
        *,
        api_key: str,
        key_id: int,
        request: AIRequest,
    ) -> GrokResponse:
        """
        Send a chat completion request to the Grok API.

        Parameters
        ----------
        api_key : str   The actual secret key to use in the Authorization header.
        key_id  : int   The DB id of this key — attached to exceptions so the
                        orchestrator knows which key failed without re-querying.
        request : AIRequest  The validated client request.

        Returns
        -------
        GrokResponse — parsed, typed response.

        Raises
        ------
        RateLimitError       on HTTP 429
        AuthenticationError  on HTTP 401 / 403
        GrokServerError      on HTTP 5xx
        GrokClientError      on other HTTP 4xx
        GrokTimeoutError     on network timeout
        GrokAPIError         on any other httpx error
        """
        payload = self._build_payload(request)

        log.debug(
            "grok_request_start",
            key_id=key_id,
            model=payload["model"],
            message_count=len(payload["messages"]),
        )

        start = time.perf_counter()

        try:
            response = await self._client.post(
                "/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.TimeoutException as exc:
            elapsed = (time.perf_counter() - start) * 1000
            log.warning(
                "grok_request_timeout",
                key_id=key_id,
                elapsed_ms=round(elapsed, 2),
            )
            raise GrokTimeoutError(
                message=f"Request timed out after {elapsed:.0f}ms",
                key_id=key_id,
            ) from exc
        except httpx.RequestError as exc:
            # Network-level errors: DNS failure, connection refused, etc.
            log.error("grok_request_network_error", key_id=key_id, error=str(exc))
            raise GrokAPIError(
                message=f"Network error: {exc}",
                key_id=key_id,
            ) from exc

        elapsed_ms = (time.perf_counter() - start) * 1000

        log.debug(
            "grok_request_done",
            key_id=key_id,
            status_code=response.status_code,
            latency_ms=round(elapsed_ms, 2),
        )

        # Map HTTP status codes to our typed exception hierarchy.
        self._raise_for_status(response, key_id=key_id)

        raw = response.json()
        return GrokResponse(raw=raw, latency_ms=elapsed_ms)

    def _build_payload(self, request: AIRequest) -> dict[str, Any]:
        """Construct the JSON body Grok expects."""
        return {
            "model": request.model or settings.GROK_DEFAULT_MODEL,
            "messages": [m.model_dump() for m in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            # stream=True requires SSE handling — not in scope for this version
            "stream": False,
        }

    def _raise_for_status(self, response: httpx.Response, key_id: int) -> None:
        """
        Convert HTTP status codes into our typed exception hierarchy.

        We deliberately handle each range separately rather than calling
        response.raise_for_status() because we need fine-grained control:
        a 429 is not the same as a 500, and the orchestrator needs to know
        the difference to choose the right recovery strategy.
        """
        code = response.status_code

        if 200 <= code < 300:
            return  # success path — no exception

        try:
            body = response.json()
            error_msg = body.get("error", {}).get("message", response.text)
        except Exception:
            error_msg = response.text

        if code == 429:
            raise RateLimitError(
                message="Rate limit exceeded",
                status_code=code,
                key_id=key_id,
                detail=error_msg,
            )

        if code in (401, 403):
            raise AuthenticationError(
                message="API key authentication failed",
                status_code=code,
                key_id=key_id,
                detail=error_msg,
            )

        if 500 <= code < 600:
            raise GrokServerError(
                message=f"Grok server error: HTTP {code}",
                status_code=code,
                key_id=key_id,
                detail=error_msg,
            )

        # All other 4xx — bad request format from our side; don't retry.
        raise GrokClientError(
            message=f"Client error: HTTP {code}",
            status_code=code,
            key_id=key_id,
            detail=error_msg,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client — call during app shutdown."""
        await self._client.aclose()


# Module-level singleton — shared across all requests in the process lifetime.
# Avoids the overhead of creating a new client (and TCP connection pool) per request.
grok_client = GrokClient()
