"""
conftest.py — shared test fixtures for the entire test suite.

Fixtures explain:

  engine / db_session:
    We create a fresh in-memory SQLite database for every test function.
    This ensures tests are hermetically isolated — one test's side effects
    (inserting a key, triggering a rate-limit) don't bleed into another.
    In-memory SQLite is fast enough that there's no need to mock the DB.

  mock_grok_client:
    We don't call the real Grok API during tests — that would be slow,
    flaky (network-dependent), and cost money. Instead, we inject a
    mock GrokClient whose `complete()` method we control via pytest's
    monkeypatch or by subclassing.

  sample_keys:
    Pre-populates the test DB with a standard set of keys so individual
    tests don't need to repeat setup boilerplate.
"""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.db.base import Base
from app.main import create_app
from app.model.api_key import APIKey, KeyStatus
from app.services.grok_client import GrokClient, GrokResponse


# ── Event loop (required for async fixtures) ──────────────────────────────────

@pytest.fixture(scope="session")
def event_loop():
    """Share one event loop across the entire test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ── Database ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db_engine():
    """
    Fresh in-memory SQLite engine per test.
    `?check_same_thread=false` is required for SQLite in async mode.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine) -> AsyncGenerator[AsyncSession, None]:
    """Yields a fresh AsyncSession backed by the test engine."""
    session_factory = async_sessionmaker(
        bind=db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    async with session_factory() as session:
        yield session
        await session.rollback()  # undo any changes after the test


# ── Sample data ───────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def sample_keys(db_session: AsyncSession) -> list[APIKey]:
    """
    Insert three test keys with different characteristics.
    Tests that need a populated key pool use this fixture.
    """
    keys = [
        APIKey(
            api_key="xai-test-key-001-aaaa",
            alias="fast-healthy",
            status=KeyStatus.ACTIVE,
            fail_count=0,
            avg_latency_ms=100.0,
            priority=0,
        ),
        APIKey(
            api_key="xai-test-key-002-bbbb",
            alias="slow-healthy",
            status=KeyStatus.ACTIVE,
            fail_count=1,
            avg_latency_ms=800.0,
            priority=0,
        ),
        APIKey(
            api_key="xai-test-key-003-cccc",
            alias="rate-limited",
            status=KeyStatus.RATE_LIMITED,
            fail_count=0,
            avg_latency_ms=200.0,
            priority=0,
        ),
    ]
    for k in keys:
        db_session.add(k)
    await db_session.flush()
    return keys


# ── Mock Grok client ──────────────────────────────────────────────────────────

def make_mock_grok_response(
    content: str = "Hello from Grok!",
    latency_ms: float = 123.4,
) -> GrokResponse:
    """Helper to build a fake GrokResponse without HTTP."""
    raw = {
        "id": "test-resp-id",
        "model": "grok-3",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }
    return GrokResponse(raw=raw, latency_ms=latency_ms)


@pytest.fixture
def mock_grok_success():
    """A GrokClient mock that always returns a successful response."""
    client = MagicMock(spec=GrokClient)
    client.complete = AsyncMock(return_value=make_mock_grok_response())
    return client


@pytest.fixture
def mock_grok_rate_limit():
    """A GrokClient mock that always raises RateLimitError."""
    from app.core.exceptions import RateLimitError

    client = MagicMock(spec=GrokClient)
    client.complete = AsyncMock(
        side_effect=RateLimitError("Rate limit hit", status_code=429, key_id=1)
    )
    return client


@pytest.fixture
def mock_grok_timeout():
    """A GrokClient mock that always raises GrokTimeoutError."""
    from app.core.exceptions import GrokTimeoutError

    client = MagicMock(spec=GrokClient)
    client.complete = AsyncMock(
        side_effect=GrokTimeoutError("Timed out", key_id=1)
    )
    return client


# ── FastAPI test client ───────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def test_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """
    Async test client for end-to-end route testing.

    We override the `get_db` dependency so routes use the test DB session
    (in-memory, isolated) instead of the real database.
    """
    from app.db.session import get_db

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session

    async with AsyncClient(app=app, base_url="http://test") as client:
        yield client
