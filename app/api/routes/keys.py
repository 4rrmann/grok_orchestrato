"""
Key Management Routes — the admin control plane.

Fixed in this version
─────────────────────
1. list_keys: by default hides soft-deleted keys (include_disabled=False).
   Add ?include_disabled=true to see everything. Add ?status=active|rate_limited|disabled
   to filter by state. This was the root cause of "deleted keys still showing up".

2. delete_key: returns a JSON body confirming what was deleted (alias + id)
   so it is obvious the delete worked, not a silent 204.

3. POST /keys/{id}/enable: new dedicated re-enable endpoint. Previously you had
   to PATCH with {status: active, is_enabled: true}. Now it is one click and also
   resets fail_count and clears cooldown automatically.

4. PATCH /keys/{id}: guard added. If the notes field looks like a real API key
   (starts with gsk_, xai-, sk-, etc.) the request is rejected with 400 so you
   never accidentally log credentials in plaintext.

5. delete_key return value changed from None (204) to a dict with alias and
   restore instructions — so the response is self-explanatory.
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import KeyValidationError
from app.core.logging_config import get_logger
from app.db.session import get_db
from app.metrics.tracker import metrics_tracker
from app.model.api_key import KeyStatus
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


# ── Admin authentication ───────────────────────────────────────────────────────

async def verify_admin_key(
    x_admin_key: str = Header(
        ...,
        description="Admin API key — set ADMIN_API_KEY in your .env file",
    ),
) -> None:
    if x_admin_key != settings.ADMIN_API_KEY:
        log.warning(
            "admin_auth_failed",
            prefix=x_admin_key[:6] if len(x_admin_key) >= 6 else "??",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin API key. Check ADMIN_API_KEY in your .env file.",
        )


# ── Create ─────────────────────────────────────────────────────────────────────

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
    """Add a Groq API key to the pool. The raw key is never returned — only masked_key is shown."""
    km = KeyManager(db)
    try:
        key = await km.create_key(data)
        log.info("admin_key_created", alias=key.alias, key_id=key.id)
        return APIKeyRead.model_validate(key)
    except KeyValidationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message)


# ── List ───────────────────────────────────────────────────────────────────────

@router.get(
    "/keys",
    response_model=APIKeyList,
    dependencies=[Depends(verify_admin_key)],
    summary="List API keys",
)
async def list_keys(
    skip: int = Query(default=0, ge=0, description="Pagination offset"),
    limit: int = Query(default=50, ge=1, le=200, description="Max results per page"),
    include_disabled: bool = Query(
        default=False,
        description="Set true to include soft-deleted/disabled keys. Default: hidden.",
    ),
    key_status: KeyStatus | None = Query(
        default=None,
        alias="status",
        description="Filter: active | rate_limited | disabled",
    ),
    db: AsyncSession = Depends(get_db),
) -> APIKeyList:
    """
    Soft-deleted keys are HIDDEN by default (include_disabled=False).
    This is why a key disappears from this list after DELETE even though
    the database row still exists for audit history.
    """
    km = KeyManager(db)
    keys, total = await km.list_keys(
        skip=skip,
        limit=limit,
        include_disabled=include_disabled,
        status_filter=key_status,
    )
    return APIKeyList(
        total=total,
        keys=[APIKeyRead.model_validate(k) for k in keys],
    )


# ── Stats ──────────────────────────────────────────────────────────────────────

@router.get(
    "/keys/stats",
    response_model=APIKeyStats,
    dependencies=[Depends(verify_admin_key)],
    summary="Fleet health statistics",
)
async def get_stats(db: AsyncSession = Depends(get_db)) -> APIKeyStats:
    """Aggregate counts across all keys. Alert if active_keys < 2."""
    km = KeyManager(db)
    stats = await km.get_fleet_stats()
    return APIKeyStats(**stats)


# ── Update (PATCH) ─────────────────────────────────────────────────────────────

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
    Partially update alias, status, priority, notes, or is_enabled.
    To re-enable a disabled key cleanly, prefer POST /admin/keys/{id}/enable
    which also resets fail_count and clears cooldown in one shot.
    """
    # Guard: reject if notes looks like a real API credential.
    # In your logs we saw the Groq key accidentally pasted into notes.
    # This prevents that — credentials in notes appear in SQL logs.
    if data.notes:
        suspicious = ("gsk_", "xai-", "sk-", "Bearer ", "token_", "key_")
        if any(data.notes.strip().startswith(p) for p in suspicious):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "The 'notes' field looks like it contains an API key or token "
                    "(starts with a known credential prefix like gsk_, xai-, sk-). "
                    "Never put raw credentials in 'notes' — they get logged in plaintext. "
                    "The api_key field is the only safe place for credential values."
                ),
            )

    km = KeyManager(db)
    key = await km.update_key(key_id, data)
    if not key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Key with id={key_id} not found.",
        )
    return APIKeyRead.model_validate(key)


# ── Re-enable ──────────────────────────────────────────────────────────────────

@router.post(
    "/keys/{key_id}/enable",
    response_model=APIKeyRead,
    dependencies=[Depends(verify_admin_key)],
    summary="Re-enable a disabled key",
)
async def enable_key(
    key_id: int,
    db: AsyncSession = Depends(get_db),
) -> APIKeyRead:
    """
    Brings a soft-deleted or auto-disabled key back into rotation.
    Resets fail_count to 0, clears cooldown_until, and sets status=active.
    The key immediately becomes eligible for request routing.
    """
    km = KeyManager(db)
    key = await km.re_enable_key(key_id)
    if not key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Key with id={key_id} not found.",
        )
    log.info("admin_key_re_enabled", key_id=key_id, alias=key.alias)
    return APIKeyRead.model_validate(key)


# ── Delete (soft) ──────────────────────────────────────────────────────────────

@router.delete(
    "/keys/{key_id}",
    dependencies=[Depends(verify_admin_key)],
    summary="Soft-delete a key",
)
async def delete_key(
    key_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Disables the key and removes it from the active pool.
    The DB row is KEPT for audit history — it just stops appearing in the
    default list and never receives traffic.

    Returns a JSON confirmation body (no longer a silent 204) so you can
    see exactly what was deleted and how to restore it.

    To permanently erase the row: DELETE directly in your database.
    To restore it: POST /admin/keys/{id}/enable
    """
    km = KeyManager(db)
    alias = await km.delete_key(key_id)
    if alias is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Key with id={key_id} not found.",
        )
    log.info("admin_key_deleted", key_id=key_id, alias=alias)
    return {
        "deleted": True,
        "key_id": key_id,
        "alias": alias,
        "message": (
            f"Key '{alias}' (id={key_id}) disabled and removed from pool. "
            f"To restore: POST /admin/keys/{key_id}/enable"
        ),
    }


# ── Metrics ────────────────────────────────────────────────────────────────────

@router.get(
    "/metrics",
    dependencies=[Depends(verify_admin_key)],
    summary="In-process request metrics",
)
async def get_metrics() -> dict:
    """Rolling p50/p95/p99 latency, success rates, per-key breakdown, uptime."""
    return metrics_tracker.get_summary()
