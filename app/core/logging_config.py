"""
Structured logging — turning log lines into queryable data.

Plain text logs like "Key 3 failed" are hard to filter in Kibana or
CloudWatch. Structured (JSON) logs let you query: status=failed AND key_id=3.

We use `structlog` for structured output and wire it into Python's
standard `logging` so third-party libraries (SQLAlchemy, httpx) also
produce structured output.

Key design: we bind context once per request (request_id, key_id) so
every log line within that request automatically carries that context —
no need to pass loggers around manually.
"""

import logging
import sys
from typing import Any
import structlog
from app.core.config import settings


def setup_logging() -> None:
    """
    Configure structlog + stdlib logging.
    Call this once at application startup (inside main.py lifespan).
    """

    # Shared processors run on every log record, in order.
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,   # injects bound context (request_id, etc.)
        structlog.stdlib.add_log_level,            # adds "level": "info"
        structlog.stdlib.add_logger_name,          # adds "logger": "app.services.orchestrator"
        structlog.processors.TimeStamper(fmt="iso"),# adds "timestamp": "2024-..."
        structlog.processors.StackInfoRenderer(),  # renders stack_info if present
    ]

    if settings.DEBUG:
        # Human-readable coloured output during local development.
        renderer: Any = structlog.dev.ConsoleRenderer(colors=True)
    else:
        # JSON lines in production — every log is one parseable JSON object.
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=shared_processors + [
            # This bridges structlog → stdlib, letting us use structlog's
            # API everywhere while stdlib handles the actual I/O.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(settings.LOG_LEVEL.upper())

    # Quieten noisy libraries — we want their errors but not their debug spam.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.DEBUG else logging.WARNING
    )


def get_logger(name: str) -> structlog.BoundLogger:
    """
    Factory used by every module:

        log = get_logger(__name__)
        log.info("key_selected", key_id=3, latency_ms=42.1)

    The `name` argument becomes the "logger" field in the JSON output,
    letting you filter logs by module.
    """
    return structlog.get_logger(name)


# ── Request-scoped context helpers ───────────────────────────────────────────

def bind_request_context(request_id: str, **kwargs: Any) -> None:
    """
    Bind values to the current async context so every subsequent
    log call within this request automatically includes them.

    Example usage in FastAPI middleware:
        bind_request_context(request_id=str(uuid4()), path="/ask-ai")
    """
    structlog.contextvars.bind_contextvars(request_id=request_id, **kwargs)


def clear_request_context() -> None:
    """Clear context at end of request to avoid leakage into next request."""
    structlog.contextvars.clear_contextvars()
