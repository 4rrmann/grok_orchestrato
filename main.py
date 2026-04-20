"""
Application entry point — where FastAPI is assembled.

This file wires together all the pieces built in other modules. Think of
it as the "main()" of the application. It should contain as little logic
as possible — its job is assembly and configuration, not computation.

Key decisions made here:

1. Lifespan context manager (instead of deprecated @app.on_event)
   The lifespan pattern is the modern FastAPI way to run code at startup
   and shutdown. Everything before `yield` runs on startup; everything
   after `yield` runs on shutdown. This guarantees cleanup even if the
   server is killed mid-request.

2. Database table creation at startup
   For production, you'd use Alembic migrations instead. But for
   development convenience, `create_all()` at startup means you can
   run the app against a fresh SQLite file and it just works.

3. Middleware ordering matters — FastAPI applies middleware in LIFO order
   (last registered = first to execute). Our RequestID middleware is
   registered last so it runs first, ensuring every subsequent middleware
   and route handler has a request_id available.

4. CORS is disabled by default — add origins explicitly; wildcard (*) is
   dangerous in production if you use cookie-based auth.
"""

import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.logging_config import (
    bind_request_context,
    clear_request_context,
    get_logger,
    setup_logging,
)
from app.db.base import Base
from app.db.session import engine
from app.api.routes import ai, keys
from app.services.grok_client import grok_client

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifespan manager.

    STARTUP (before yield):
      - Configure logging first so all subsequent startup logs are structured
      - Create DB tables (dev convenience; use Alembic in production)
      - Log that we're ready

    SHUTDOWN (after yield):
      - Close the shared httpx client (drains connection pool gracefully)
      - Flush any pending log records
    """
    # ── Startup ───────────────────────────────────────────────────────────────
    setup_logging()
    log.info("app_starting", name=settings.APP_NAME, version=settings.APP_VERSION)

    async with engine.begin() as conn:
        # create_all is idempotent — safe to call on every restart
        await conn.run_sync(Base.metadata.create_all)
    log.info("database_tables_ready")

    log.info("app_ready", debug=settings.DEBUG)

    yield  # ← application runs here

    # ── Shutdown ──────────────────────────────────────────────────────────────
    log.info("app_shutting_down")
    await grok_client.close()
    log.info("http_client_closed")


def create_app() -> FastAPI:
    """
    Application factory — returns a configured FastAPI instance.

    Using a factory function (rather than a module-level `app = FastAPI()`)
    makes the app easier to test: tests can call `create_app()` to get a
    fresh instance with different settings.
    """
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description=(
            "Intelligent API orchestration layer for managing multiple Grok API keys. "
            "Provides smart load balancing, automatic failover, and performance tracking."
        ),
        lifespan=lifespan,
        # Disable default /docs in production if you don't want it publicly accessible.
        docs_url="/docs" if settings.DEBUG else None,
        redoc_url="/redoc" if settings.DEBUG else None,
    )

    # ── Middleware ─────────────────────────────────────────────────────────────

    # CORS — configure allowed origins for your frontend domain(s)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.DEBUG else [],  # restrict in production!
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next) -> Response:
        """
        Assigns a unique request_id to every incoming request and attaches
        it to the structured logging context so every log line within this
        request carries the same ID. Also echoes the ID in the response
        header so clients can correlate their calls with our server logs.
        """
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        bind_request_context(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            clear_request_context()  # prevent context leaking to next request

    @app.middleware("http")
    async def log_requests(request: Request, call_next) -> Response:
        """Structured access log for every request — replaces uvicorn's default logs."""
        response = await call_next(request)
        log.info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
        )
        return response

    # ── Global exception handler ───────────────────────────────────────────────

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """
        Last-resort handler. If an exception escapes all route handlers
        (should be rare), this returns a clean JSON 500 instead of an
        ugly HTML traceback.
        """
        log.exception("unhandled_exception", path=request.url.path, error=str(exc))
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error", "detail": str(exc) if settings.DEBUG else None},
        )

    # ── Routers ───────────────────────────────────────────────────────────────

    app.include_router(
        ai.router,
        prefix="/v1",
        tags=["AI Completions"],
    )
    app.include_router(
        keys.router,
        prefix="/admin",
        tags=["Key Management"],
    )

    # ── Health check ──────────────────────────────────────────────────────────

    @app.get("/health", tags=["System"])
    async def health_check() -> dict:
        """
        Lightweight health check for load balancers and container orchestrators
        (ECS, Kubernetes liveness/readiness probes). Should return quickly
        with no DB calls — just confirms the process is alive.
        """
        return {"status": "ok", "version": settings.APP_VERSION}

    return app


# Module-level app instance — imported by uvicorn:
#   uvicorn app.main:app --reload
app = create_app()
