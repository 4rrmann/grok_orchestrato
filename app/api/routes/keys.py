"""
Key Management Routes — the admin control plane.

These routes let operators manage the key fleet: register new keys,
inspect their health, update settings, and manually disable/enable them.

Security model: all routes in this file are protected by an API key
header. This is intentional — these are administrative operations that
could disrupt service if misused. The `verify_admin_key` dependency
enforces this on every request.

In a production system you might replace the static key check with:
  - OAuth2 scopes (if you already have an auth server)
  - mTLS between internal services
  - AWS IAM if this runs in a VPC
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import KeyValidationError, NoAvailableKeyError
from app.core.logging_config import get_logger
from app.db.session import get_db
from app.metrics.tracker import metrics_tracker
from app.schemas.api_key import (
    APIKeyCreate,
    APIKeyList,
    APIKeyRead,
    APIKeyStats,
    APIKeyUpdate,
)
from app.services.key_manager import KeyManager

log = get_logger(__name__)
router = APIRouter()


async def verify_admin_key(
    x_admin_key: str = Header(..., description="Admin API key for management endpoints"),
) -> None:
    """
    Dependency that validates the admin key header.

    We use `Header(...)` (required) rather than `Header(None)` (optional)
    so FastAPI returns a 422 automatically if the header is missing,
    before our validation logic even runs.
    """
    if x_admin_key != settings.ADMIN_API_KEY:
        log.warning("admin_auth_failed", provided_key_prefix=x_admin_key[:4] if x_admin_key else "")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin API key",
        )


@router.post(
    "/keys",
    response_model=APIKeyRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_admin_key)],
    summary="Register a new API key",
)
async def create_key(
    data: APIKeyCreate,
    db: AsyncSession = Depends(get_db),
) -> APIKeyRead:
    """Register a new Grok API key into the orchestration pool."""
    km = KeyManager(db)
    try:
        key = await km.create_key(data)
        log.info("admin_key_created", alias=key.alias)
        return APIKeyRead.model_validate(key)
    except KeyValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.message,
        )


@router.get(
    "/keys",
    response_model=APIKeyList,
    dependencies=[Depends(verify_admin_key)],
    summary="List all API keys",
)
async def list_keys(
    skip: int = Query(default=0, ge=0, description="Pagination offset"),
    limit: int = Query(default=50, ge=1, le=200, description="Max results"),
    db: AsyncSession = Depends(get_db),
) -> APIKeyList:
    """Return a paginated list of all registered keys and their current state."""
    km = KeyManager(db)
    keys, total = await km.list_keys(skip=skip, limit=limit)
    return APIKeyList(
        total=total,
        keys=[APIKeyRead.model_validate(k) for k in keys],
    )


@router.get(
    "/keys/stats",
    response_model=APIKeyStats,
    dependencies=[Depends(verify_admin_key)],
    summary="Fleet health statistics",
)
async def get_stats(db: AsyncSession = Depends(get_db)) -> APIKeyStats:
    """
    Return aggregate statistics across the entire key fleet.
    Useful for dashboards and alerting (e.g., alert if active_keys < 2).
    """
    km = KeyManager(db)
    stats = await km.get_fleet_stats()
    return APIKeyStats(**stats)


@router.patch(
    "/keys/{key_id}",
    response_model=APIKeyRead,
    dependencies=[Depends(verify_admin_key)],
    summary="Update a key's settings",
)
async def update_key(
    key_id: int,
    data: APIKeyUpdate,
    db: AsyncSession = Depends(get_db),
) -> APIKeyRead:
    """
    Partially update a key. Use this to:
      - Change alias or notes
      - Manually set status (e.g., re-enable a disabled key)
      - Adjust priority weight
    """
    km = KeyManager(db)
    key = await km.update_key(key_id, data)
    if not key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Key not found")
    return APIKeyRead.model_validate(key)


@router.delete(
    "/keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(verify_admin_key)],
    summary="Disable and remove a key",
)
async def delete_key(
    key_id: int,
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    Soft-delete a key (sets is_enabled=False, status=disabled).
    The row is retained for audit history. Use PATCH to re-enable it.
    """
    km = KeyManager(db)
    deleted = await km.delete_key(key_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Key not found")


@router.get(
    "/metrics",
    dependencies=[Depends(verify_admin_key)],
    summary="In-process request metrics",
)
async def get_metrics() -> dict:
    """
    Return in-process performance metrics: latency percentiles,
    success rates, per-key breakdown, uptime. This is the data you'd
    typically export to Prometheus or display in a Grafana dashboard.
    """
    return metrics_tracker.get_summary()
