"""
Exception hierarchy — the error contract of the entire system.

Think of these as typed signals. Instead of catching generic `Exception`
and guessing what went wrong, every layer of the stack raises a specific
class. This lets the orchestrator make intelligent decisions:

  OrchestratorError          ← base for all our custom errors
  ├── NoAvailableKeyError     ← all keys are exhausted; return 503
  ├── AllRetriesExhaustedError← tried N keys, all failed
  └── GrokAPIError            ← something went wrong talking to Grok
      ├── RateLimitError      ← 429 → apply cooldown, retry with another key
      ├── TimeoutError        ← request timed out → increment fail count
      ├── AuthenticationError ← 401 → key is invalid; disable it permanently
      └── GrokServerError     ← 5xx from Grok → transient; retry once
"""

from __future__ import annotations
from typing import Optional


# ── Base ─────────────────────────────────────────────────────────────────────

class OrchestratorError(Exception):
    """Root exception for all application-level errors."""

    def __init__(self, message: str, detail: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail  # extra context for logs

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(message={self.message!r})"


# ── Key Management Errors ────────────────────────────────────────────────────

class NoAvailableKeyError(OrchestratorError):
    """
    Raised when the KeyManager cannot find a single usable key.
    All keys are either in cooldown, disabled, or the pool is empty.
    HTTP translation: 503 Service Unavailable.
    """
    pass


class AllRetriesExhaustedError(OrchestratorError):
    """
    Raised after the orchestrator has tried MAX_RETRIES different keys
    and none produced a successful response.
    HTTP translation: 502 Bad Gateway.
    """

    def __init__(self, attempts: int, last_error: Optional[str] = None) -> None:
        super().__init__(
            message=f"All {attempts} retry attempt(s) failed.",
            detail=last_error,
        )
        self.attempts = attempts
        self.last_error = last_error


# ── Grok API Errors ──────────────────────────────────────────────────────────

class GrokAPIError(OrchestratorError):
    """
    Base class for any error originating from the Grok HTTP API.
    Always carries the HTTP status code so the orchestrator can
    classify failures without inspecting raw response text.
    """

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        key_id: Optional[int] = None,
        detail: Optional[str] = None,
    ) -> None:
        super().__init__(message, detail)
        self.status_code = status_code
        self.key_id = key_id      # which key caused this error (for targeted state update)


class RateLimitError(GrokAPIError):
    """
    HTTP 429 — the API key has hit its rate limit.
    Correct response: put key in cooldown, try another key immediately.
    """
    pass


class AuthenticationError(GrokAPIError):
    """
    HTTP 401/403 — the API key is invalid or revoked.
    Correct response: permanently disable this key; do not retry with it.
    """
    pass


class GrokTimeoutError(GrokAPIError):
    """
    The HTTP request timed out before Grok responded.
    Correct response: increment fail_count; retry with a different key.
    Note: named GrokTimeoutError to avoid shadowing Python's built-in TimeoutError.
    """
    pass


class GrokServerError(GrokAPIError):
    """
    HTTP 5xx from Grok — transient server-side problem.
    Correct response: increment fail_count; retry with a different key.
    """
    pass


class GrokClientError(GrokAPIError):
    """
    HTTP 4xx (other than 429/401/403) — malformed request from our side.
    Correct response: do NOT retry (retrying won't fix a bad request); surface to caller.
    """
    pass


# ── Validation Errors ────────────────────────────────────────────────────────

class KeyValidationError(OrchestratorError):
    """Raised when a submitted API key fails format validation."""
    pass
