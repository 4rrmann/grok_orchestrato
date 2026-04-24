"""
Orchestrator — the central decision engine that ties everything together.

The orchestrator implements the "observe → decide → act → learn" loop:

  OBSERVE:  ask KeyManager for the best current key
  DECIDE:   determine if we should try it (or give up entirely)
  ACT:      delegate the actual API call to GrokClient
  LEARN:    tell KeyManager what happened so it can update state

Why is this a separate layer from the FastAPI route?
  Routes should be thin — their job is HTTP concerns (parsing requests,
  returning responses, setting status codes). The orchestrator holds
  complex business logic: retry policies, failure classification,
  exception-to-action mapping. If you ever add a CLI, a Celery worker,
  or a gRPC interface, they can all reuse this same orchestrator without
  touching HTTP code.

Retry strategy — "try N different keys, never the same one twice"
  We don't retry the same key multiple times (that would be pointless for
  rate limits and counterproductive for timeouts). Instead, we rotate to
  a fresh key on each retry. The set `tried_key_ids` tracks which keys
  we've already attempted so the KeyManager knows to skip them.

  Future improvement: pass `exclude_ids` to the DB query so we never
  even load already-tried keys into memory.
"""

from __future__ import annotations

import time
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    AllRetriesExhaustedError,
    AuthenticationError,
    GrokClientError,
    GrokServerError,
    GrokTimeoutError,
    NoAvailableKeyError,
    OrchestratorError,
    RateLimitError,
)
from app.core.logging_config import get_logger
from app.model.api_key import APIKey
from app.schemas.request import AIRequest, AIResponse, UsageStats
from app.services.grok_client import GrokClient, GrokResponse, grok_client
from app.services.key_manager import KeyManager

log = get_logger(__name__)


class Orchestrator:
    """
    Stateless request handler that coordinates KeyManager and GrokClient.

    "Stateless" here means the Orchestrator itself holds no mutable state
    between requests — all persistent state lives in the database (managed
    by KeyManager). This makes the orchestrator safe to use concurrently
    and easy to test.
    """

    def __init__(
        self,
        db: AsyncSession,
        client: GrokClient = grok_client,  # injectable for testing
    ) -> None:
        self.key_manager = KeyManager(db)
        self.client = client

    async def handle_request(
        self,
        request: AIRequest,
        request_id: str,
    ) -> AIResponse:
        """
        Main entry point. Implements the full request lifecycle:

          1. Determine how many retries to allow
          2. Loop: pick a key → call Grok → on failure, classify and handle
          3. On success, update state and build response
          4. If all retries exhausted, raise AllRetriesExhaustedError

        Parameters
        ----------
        request    : AIRequest — validated client request
        request_id : str — correlation ID for log tracing

        Returns
        -------
        AIResponse — enriched response including metadata

        Raises
        ------
        NoAvailableKeyError       — no keys at all; 503
        AllRetriesExhaustedError  — tried everything; 502
        GrokClientError           — bad request; 400 (don't retry)
        """
        max_retries = request.max_retries or settings.MAX_RETRIES
        tried_key_ids: set[int] = set()
        last_error: Optional[str] = None
        attempt = 0

        overall_start = time.perf_counter()

        while attempt < max_retries:
            attempt += 1
            key: Optional[APIKey] = None

            try:
                # ── Step 1: Select best available key ─────────────────────────
                key = await self._select_key(exclude_ids=tried_key_ids)
                tried_key_ids.add(key.id)

                log.info(
                    "orchestrator_attempt",
                    request_id=request_id,
                    attempt=attempt,
                    max_retries=max_retries,
                    key_id=key.id,
                    key_alias=key.alias,
                )

                # ── Step 2: Call the Grok API ─────────────────────────────────
                grok_response = await self.client.complete(
                    api_key=key.api_key,
                    key_id=key.id,
                    request=request,
                )

                # ── Step 3: Record success and return ─────────────────────────
                await self.key_manager.record_success(key, grok_response.latency_ms)

                overall_latency = (time.perf_counter() - overall_start) * 1000

                log.info(
                    "orchestrator_success",
                    request_id=request_id,
                    key_id=key.id,
                    key_alias=key.alias,
                    attempts=attempt,
                    latency_ms=round(overall_latency, 2),
                )

                return self._build_response(grok_response, key, attempt, overall_latency)

            except RateLimitError:
                # ── Rate limited: cooldown this key, try another ───────────────
                if key:
                    await self.key_manager.record_rate_limit(key)
                last_error = f"Key '{key.alias if key else '?'}' rate limited"
                log.warning(
                    "orchestrator_rate_limit",
                    request_id=request_id,
                    attempt=attempt,
                    key_id=key.id if key else None,
                )
                # Continue to next iteration — try a different key

            except AuthenticationError:
                # ── Invalid key: disable immediately, try another ──────────────
                if key:
                    await self.key_manager.record_auth_failure(key)
                last_error = f"Key '{key.alias if key else '?'}' auth failed"
                log.error(
                    "orchestrator_auth_failure",
                    request_id=request_id,
                    attempt=attempt,
                    key_id=key.id if key else None,
                )
                # Continue — other keys may still be valid

            except (GrokTimeoutError, GrokServerError):
                # ── Transient failure: increment fail count, try another ────────
                if key:
                    await self.key_manager.record_failure(key)
                last_error = f"Key '{key.alias if key else '?'}' transient failure"
                log.warning(
                    "orchestrator_transient_failure",
                    request_id=request_id,
                    attempt=attempt,
                    key_id=key.id if key else None,
                )
                # Continue — next key might succeed

            except GrokClientError as exc:
                # ── Bad request: our payload is wrong — do NOT retry ───────────
                # Retrying with a different key won't fix a malformed request.
                # Surface immediately to the caller.
                log.error(
                    "orchestrator_client_error",
                    request_id=request_id,
                    key_id=key.id if key else None,
                    detail=exc.detail,
                )
                raise  # propagates to FastAPI route → 400 response

            except NoAvailableKeyError:
                # ── No keys left in the pool ───────────────────────────────────
                log.error(
                    "orchestrator_no_keys",
                    request_id=request_id,
                    attempt=attempt,
                    tried=list(tried_key_ids),
                )
                raise  # propagates to FastAPI route → 503 response

        # ── Loop exhausted: all retries failed ────────────────────────────────
        raise AllRetriesExhaustedError(attempts=attempt, last_error=last_error)

    async def _select_key(self, exclude_ids: set[int]) -> APIKey:
        """
        Get the best available key, excluding already-tried ones.

        We call get_best_available_key() repeatedly after removing tried
        keys from the candidate pool. A cleaner approach would pass
        `exclude_ids` to the DB query, but the current implementation
        keeps the KeyManager API simple and this inner loop is cheap
        (few keys typically in rotation).

        For fleets with hundreds of keys, add an `exclude_ids` parameter
        to `_fetch_eligible_keys` and pass it through.
        """
        key = await self.key_manager.get_best_available_key()

        # If the "best" key was already tried, we need another one.
        # This loop will eventually raise NoAvailableKeyError when
        # get_best_available_key runs out of candidates.
        attempts = 0
        while key.id in exclude_ids:
            attempts += 1
            if attempts > 20:  # safety valve — should never happen in practice
                raise NoAvailableKeyError("Could not find an untried key")
            key = await self.key_manager.get_best_available_key()

        return key

    @staticmethod
    def _build_response(
        grok: GrokResponse,
        key: APIKey,
        attempts: int,
        overall_latency_ms: float,
    ) -> AIResponse:
        """
        Construct the enriched AIResponse that we return to the client.

        We deliberately expose the key alias (not the key value) and
        orchestration metadata. This helps API consumers understand how
        their request was served and is invaluable during incident response.
        """
        return AIResponse(
            content=grok.content,
            model=grok.model,
            usage=UsageStats(
                prompt_tokens=grok.prompt_tokens,
                completion_tokens=grok.completion_tokens,
                total_tokens=grok.total_tokens,
            ),
            key_alias=key.alias,
            attempts=attempts,
            latency_ms=round(overall_latency_ms, 2),
            finish_reason=grok.finish_reason or None,
            raw_response_id=grok.response_id or None,
        )
