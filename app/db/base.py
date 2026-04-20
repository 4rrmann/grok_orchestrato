"""
Declarative base — the shared ancestor for all SQLAlchemy models.

Every model (table) inherits from `Base`. SQLAlchemy uses this common
parent to keep track of all models during `metadata.create_all()` and
Alembic migrations.

We also define a `TimestampMixin` here — a reusable set of
created_at / updated_at columns that every table should have.
These are invaluable in production for debugging ("when did this key's
status change?") and auditing.
"""

from datetime import datetime, timezone
from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Return timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """
    All models inherit from this. SQLAlchemy's DeclarativeBase
    gives us the modern (2.0-style) mapped_column() API with
    full type safety through Python type hints.
    """
    pass


class TimestampMixin:
    """
    Reusable mixin that adds audit timestamps to any model.

    `server_default=func.now()` means the DB sets the value on INSERT,
    which is safer than relying on application-level timing (clock skew,
    application bugs, etc.).

    `onupdate=func.now()` means the DB updates `updated_at` automatically
    on every UPDATE statement — zero risk of forgetting to set it.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
