"""
AI Route — the public-facing endpoint that clients call.

This file is deliberately thin. The route handler does exactly three things:
  1. Accept and validate the HTTP request (Pydantic does this automatically)
  2. Delegate to the Orchestrator (which holds all business logic)
  3. Convert Orchestrator results/exceptions into HTTP responses

Notice there is no retry logic here, no key selection, no latency tracking.
All of that lives in the service layer. The route's only job is HTTP plumbing.

We map our custom exceptions to specific HTTP status codes here because
HTTP semantics are an API-layer concern:
  NoAvailableKeyError       → 503 (Service Unavailable — the upstream pool is empty)
  AllRetriesExhaustedError  → 502 (Bad Gateway — we tried, upstream kept failing)
  GrokClientError           → 400 (Bad Request — the client's payload was invalid)
  Unexpected                → 500 (Internal Server Error — something we didn't anticipate)
"""

import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    AllRetriesExhaustedError,
    GrokClientError,
    NoAvailableKeyError,
)
from app.core.logging_config import bind_request_context, get_logger
from app.db.session import get_db
from app.metrics.tracker import metrics_tracker
from app.schemas.request import AIRequest, AIResponse, ErrorResponse
from app.services.orchestrator import Orchestrator

log = get_logger(__name__)
router = APIRouter()


@router.post(
    "/ask-ai",
    response_model=AIResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid request payload"},
        503: {"model": ErrorResponse, "description": "No API keys available"},
        502: {"model": ErrorResponse, "description": "All upstream retries failed"},
    },
    summary="Submit an AI completion request",
    description=(
        "Accepts a chat completion request and routes it through the most "
        "suitable available Grok API key. Automatically retries with fallback "
        "keys on failure. Returns enriched response metadata including which "
        "key was used and end-to-end latency."
    ),
)
async def ask_ai(
    request: AIRequest,
    db: AsyncSession = Depends(get_db),
) -> AIResponse:
    """
    The single public endpoint. The `Depends(get_db)` injection ensures
    this route gets its own DB session that is committed on success and
    rolled back on exception — managed entirely by the `get_db` generator.
    """
    request_id = str(uuid.uuid4())
    # Bind the request_id to the structlog context — every log line from
    # this point until the request ends will automatically include it.
    bind_request_context(request_id=request_id, path="/ask-ai")

    log.info(
        "ai_request_received",
        request_id=request_id,
        message_count=len(request.messages),
        model=request.model,
    )

    orchestrator = Orchestrator(db=db)

    try:
        response = await orchestrator.handle_request(request, request_id=request_id)

        # Record metrics asynchronously — we don't await this carefully
        # because a metrics failure should never impact the user response.
        await metrics_tracker.record_request(
            key_id=0,          # we don't track id here; alias is sufficient
            key_alias=response.key_alias,
            latency_ms=response.latency_ms,
            success=True,
            attempts=response.attempts,
        )

        return response

    except NoAvailableKeyError as exc:
        await metrics_tracker.record_request(
            key_id=0, key_alias="none", latency_ms=0,
            success=False, attempts=0, error_type="no_available_key",
        )
        log.error("route_no_available_key", request_id=request_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": exc.message, "request_id": request_id},
        )

    except AllRetriesExhaustedError as exc:
        await metrics_tracker.record_request(
            key_id=0, key_alias="exhausted", latency_ms=0,
            success=False, attempts=exc.attempts, error_type="all_retries_exhausted",
        )
        log.error(
            "route_all_retries_exhausted",
            request_id=request_id,
            attempts=exc.attempts,
            last_error=exc.last_error,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": exc.message,
                "detail": exc.last_error,
                "request_id": request_id,
            },
        )

    except GrokClientError as exc:
        log.warning(
            "route_client_error",
            request_id=request_id,
            detail=exc.detail,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": exc.message, "detail": exc.detail, "request_id": request_id},
        )

    except Exception as exc:
        # Catch-all: something we didn't anticipate. Log with full traceback.
        log.exception(
            "route_unexpected_error",
            request_id=request_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "An unexpected error occurred", "request_id": request_id},
        )
