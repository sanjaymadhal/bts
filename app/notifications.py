"""Notification REST surface.

Endpoints:
  POST   /notifications/token            — register a push token
  DELETE /notifications/token            — remove a push token
  GET    /notifications                 — list the caller's inbox
  POST   /notifications/{id}/read       — mark one row read
  POST   /notifications/read-all        — mark every row read
  GET    /notifications/unread-count    — short-form count

The inbox render path (parent + admin) hits GET /notifications on
mount and then keeps itself fresh via Supabase Realtime on the
`notifications` table. This REST surface is the source of truth for
initial fetch + mark-as-read side-effects.

All endpoints require a valid bearer token. Reading + writing is
always scoped to the caller — admins do not see parents' inboxes
and vice versa.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from .deps import CurrentUser, get_current_user, get_supabase_admin

router = APIRouter()
_logger = logging.getLogger("trackr.notifications")


def _get_data(res: Any) -> Any:
    if res is None:
        return None
    return getattr(res, "data", None)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PushTokenIn(BaseModel):
    token: str = Field(min_length=1, max_length=255)
    platform: Literal["ios", "android", "web"]


class PushTokenOut(BaseModel):
    ok: bool = True


class NotificationOut(BaseModel):
    id: str
    kind: str
    bus_id: Optional[str] = None
    student_id: Optional[str] = None
    title: str
    body: str
    data: dict[str, Any] = Field(default_factory=dict)
    read_at: Optional[str] = None
    created_at: str


class UnreadCountOut(BaseModel):
    unread: int


# ---------------------------------------------------------------------------
# Push token registration
# ---------------------------------------------------------------------------


@router.post("/token", response_model=PushTokenOut)
async def register_push_token(
    body: PushTokenIn,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
) -> PushTokenOut:
    """Upsert the caller's Expo push token.

    Idempotent — repeated POSTs with the same `token` are no-ops.
    The row is keyed on `user_id` so a user only ever has one
    active device; if the same user signs in on two phones we keep
    the latest. (A multi-device migration later would relax this.)
    """
    payload = {
        "user_id": user.user_id,
        "token": body.token,
        "platform": body.platform,
        "updated_at": "now()",
    }
    try:
        # Postgres UPSERT — equivalent to .upsert() in supabase-py.
        # We use upsert() here so the row PK violation never happens.
        supabase.table("push_tokens").upsert(payload).execute()
        return PushTokenOut(ok=True)
    except Exception as exc:
        _logger.warning(
            "push token upsert failed for user=%s: %s", user.user_id, exc
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not register the push token. Try again later.",
        ) from exc


@router.delete("/token", response_model=PushTokenOut)
async def unregister_push_token(
    body: PushTokenIn,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
) -> PushTokenOut:
    """Remove a push token on sign-out.

    Matches on `user_id + token` so a stale request for someone
    else's token never deletes the wrong row.
    """
    try:
        supabase.table("push_tokens").delete().eq("user_id", user.user_id).eq(
            "token", body.token
        ).execute()
        return PushTokenOut(ok=True)
    except Exception as exc:
        _logger.warning(
            "push token delete failed for user=%s: %s", user.user_id, exc
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not clear the push token.",
        ) from exc


# ---------------------------------------------------------------------------
# Inbox read + write
# ---------------------------------------------------------------------------


@router.get("", response_model=list[NotificationOut])
def list_notifications(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    unread: bool = False,
    limit: int = 100,
    supabase=Depends(get_supabase_admin),
) -> list[NotificationOut]:
    """Paginated inbox for the caller.

    `unread=true` filters to rows where `read_at IS NULL`. `limit`
    is capped at 200 so a malicious client can't ask for the entire
    table.
    """
    safe_limit = max(1, min(int(limit), 200))
    try:
        query = (
            supabase.table("notifications")
            .select(
                "id, kind, bus_id, student_id, title, body, data, read_at, created_at"
            )
            .eq("user_id", user.user_id)
            .order("created_at", desc=True)
            .limit(safe_limit)
        )
        if unread:
            query = query.is_("read_at", "null")
        res = query.execute()
        rows = _get_data(res) or []
    except Exception as exc:
        _logger.warning("list notifications failed for %s: %s", user.user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not load notifications.",
        ) from exc
    return [_shape(r) for r in rows]


@router.get("/unread-count", response_model=UnreadCountOut)
def unread_count(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
) -> UnreadCountOut:
    """Return the caller's unread notification count.

    Cheaper than the inbox list — the UI calls this on app focus to
    update the bell badge without re-paginating the whole feed.
    """
    try:
        # We can't .count() through supabase-py without a head/row
        # pair — minimal query: select just the read_at column so the
        # payload is tiny.
        res = (
            supabase.table("notifications")
            .select("read_at")
            .eq("user_id", user.user_id)
            .is_("read_at", "null")
            .execute()
        )
        rows = _get_data(res) or []
    except Exception as exc:
        _logger.warning("unread-count failed for %s: %s", user.user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not count unread notifications.",
        ) from exc
    return UnreadCountOut(unread=len(rows))


@router.post("/{notification_id}/read", response_model=UnreadCountOut)
async def mark_notification_read(
    notification_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
) -> UnreadCountOut:
    """Mark one notification read. Idempotent — sets read_at to now()."""
    try:
        supabase.table("notifications").update(
            {"read_at": "now()"}
        ).eq("id", notification_id).eq("user_id", user.user_id).execute()
    except Exception as exc:
        _logger.warning(
            "mark-read failed for %s/%s: %s", user.user_id, notification_id, exc
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not update the notification.",
        ) from exc
    return await _recount_unread(supabase, user.user_id)


@router.post("/read-all", response_model=UnreadCountOut)
async def mark_all_read(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
) -> UnreadCountOut:
    """Mark every caller's notification read in one round-trip."""
    try:
        supabase.table("notifications").update(
            {"read_at": "now()"}
        ).eq("user_id", user.user_id).is_("read_at", "null").execute()
    except Exception as exc:
        _logger.warning("mark-all-read failed for %s: %s", user.user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not update notifications.",
        ) from exc
    return UnreadCountOut(unread=0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _shape(row: dict[str, Any]) -> NotificationOut:
    """Map a raw `notifications` row to the spec's shape.

    `created_at` / `read_at` come back as ISO strings from PostgREST.
    We coerce datetimes to strings for the JSON response.
    """
    return NotificationOut(
        id=row["id"],
        kind=row["kind"],
        bus_id=row.get("bus_id"),
        student_id=row.get("student_id"),
        title=row.get("title") or "",
        body=row.get("body") or "",
        data=row.get("data") or {},
        read_at=str(row["read_at"]) if row.get("read_at") else None,
        created_at=str(row["created_at"]),
    )


async def _recount_unread(supabase, user_id: str) -> UnreadCountOut:
    try:
        res = (
            supabase.table("notifications")
            .select("read_at")
            .eq("user_id", user_id)
            .is_("read_at", "null")
            .execute()
        )
        rows = _get_data(res) or []
        return UnreadCountOut(unread=len(rows))
    except Exception:
        return UnreadCountOut(unread=0)
