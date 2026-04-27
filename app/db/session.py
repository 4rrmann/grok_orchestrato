"""
Database session management — the gateway between Python objects and SQL rows.

We use SQLAlchemy's async engine because FastAPI is async-native. A sync
engine would block the event loop during every DB query, defeating the
purpose of async entirely.

The `AsyncSessionLocal` factory produces sessions on demand. We never
share a session between requests — each request gets its own session,
uses it, and returns it to the pool when done (context manager handles this).

Connection pooling explanation:
  - pool_size=10: keep 10 connections open and warm (reused across requests)
  - max_overflow=20: allow 20 extra connections under peak load
  - pool_pre_ping=True: send a cheap "SELECT 1" before reusing a connection
    to detect stale connections (e.g., DB restarted while app was running)
"""

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    AsyncEngine,
    create_async_engine,
    async_sessionmaker,
)
from app.core.config import settings
from app.core.logging_config import get_logger

log = get_logger(__name__)


def _build_engine() -> AsyncEngine:
    """
    Build the async SQLAlchemy engine.
    SQLite gets different pool settings because it doesn't support
    concurrent writes — NullPool is safest for SQLite in async mode.
    """
    url = settings.DATABASE_URL
    is_sqlite = url.startswith("sqlite")

    connect_args = {"check_same_thread": False} if is_sqlite else {}

    kwargs: dict = {
        # echo=True logs every SQL statement.
        # hide_parameters=True prevents SQLAlchemy from logging the actual
        # bound parameter values — so your API keys never appear in the
        # terminal output even in DEBUG mode. Always keep this True.
        "echo": settings.DEBUG,
        "hide_parameters": True,
        "connect_args": connect_args,
    }

    if not is_sqlite:
        kwargs.update({
            "pool_size": 10,
            "max_overflow": 20,
            "pool_pre_ping": True,
            "pool_recycle": 3600,        # recycle connections after 1h to avoid stale TCP
        })

    log.info("database_engine_init", url=url.split("@")[-1])  # log host, never credentials
    return create_async_engine(url, **kwargs)


engine: AsyncEngine = _build_engine()

# session factory — call AsyncSessionLocal() to get a new session
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # keep objects readable after commit without re-querying
    autoflush=False,         # we control when SQL is flushed (inside service methods)
    autocommit=False,
)


async def get_db() -> AsyncSession:
    """
    FastAPI dependency that yields a database session per request.

    Usage in a route:
        async def my_route(db: AsyncSession = Depends(get_db)):
            ...

    The `finally` block ensures the session is always closed — even if
    the route handler raises an exception.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
