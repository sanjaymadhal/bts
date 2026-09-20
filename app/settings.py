"""School-scoped settings.

Single row per school. PATCH is partial — only present fields are
written. The frontend uses this for school_name, auto_assign_stops,
language, notifications_enabled, and default_alert_radius_m.
"""

from __future__ import annotations

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .deps import (
    CurrentUser,
    get_current_user,
    get_supabase_admin,
    get_supabase_user,
    load_app_role,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SchoolSettingsOut(BaseModel):
    school_id: str
    school_name: str
    school_code: str
    notifications_enabled: bool
    auto_assign_stops: bool
    language: str = Field(pattern=r"^(en|hi|kn)$")
    default_alert_radius_m: int


class SchoolSettingsPatch(BaseModel):
    notifications_enabled: Optional[bool] = None
    auto_assign_stops: Optional[bool] = None
    language: Optional[str] = Field(default=None, pattern=r"^(en|hi|kn)$")
    default_alert_radius_m: Optional[int] = Field(default=None, ge=50, le=5000)
    school_name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    # School code lives on the `schools` row (not `school_settings`) but
    # is exposed through PATCH /settings so the admin client has a single
    # endpoint to update. Pattern is upper-case letters/digits/dashes —
    # the same shape the seeded example uses ("DPS-EAST").
    school_code: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=40,
        pattern=r"^[A-Z0-9][A-Z0-9-]{0,39}$",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load(school_id: str, supabase) -> SchoolSettingsOut:
    settings_row = (
        supabase.table("school_settings")
        .select("school_id, notifications_enabled, auto_assign_stops, language, default_alert_radius_m")
        .eq("school_id", school_id)
        .maybe_single()
        .execute()
        .data
    )
    school_row = (
        supabase.table("schools")
        .select("id, name, code")
        .eq("id", school_id)
        .maybe_single()
        .execute()
        .data
    )
    if not school_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="School not found.",
        )
    if not settings_row:
        # Sensible defaults if the school_settings row hasn't been seeded yet.
        return SchoolSettingsOut(
            school_id=school_row["id"],
            school_name=school_row["name"],
            school_code=school_row["code"],
            notifications_enabled=True,
            auto_assign_stops=False,
            language="en",
            default_alert_radius_m=250,
        )
    return SchoolSettingsOut(
        school_id=school_row["id"],
        school_name=school_row["name"],
        school_code=school_row["code"],
        notifications_enabled=settings_row["notifications_enabled"],
        auto_assign_stops=settings_row["auto_assign_stops"],
        language=settings_row["language"],
        default_alert_radius_m=settings_row["default_alert_radius_m"],
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=SchoolSettingsOut)
def get_settings_route(
    _user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
    settings: Settings = Depends(get_settings),
) -> SchoolSettingsOut:
    school_id = _resolve_school_id(supabase, settings)
    return _load(school_id, supabase)


@router.patch("", response_model=SchoolSettingsOut)
def patch_settings(
    body: SchoolSettingsPatch,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
    settings: Settings = Depends(get_settings),
) -> SchoolSettingsOut:
    school_id = _resolve_school_id(supabase, settings)

    # Resolve the caller's APP role once. Routes that touch the
    # `schools` row (school_name, school_code) require admin.
    if user.app_role is None:
        user.app_role = load_app_role(user, supabase)
    is_admin = user.app_role == "admin"

    # Write paths on the `schools` row first — name and code both
    # belong there.
    school_update: dict[str, Any] = {}
    if body.school_name is not None:
        if not is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admins can change the school name.",
            )
        school_update["name"] = body.school_name
    if body.school_code is not None:
        if not is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admins can change the school code.",
            )
        school_update["code"] = body.school_code
    if school_update:
        supabase.table("schools").update(school_update).eq("id", school_id).execute()

    settings_patch: dict[str, Any] = {
        k: v
        for k, v in body.model_dump(exclude_none=True).items()
        if k not in ("school_name", "school_code")
    }
    if settings_patch:
        supabase.table("school_settings").update(settings_patch).eq("school_id", school_id).execute()
    return _load(school_id, supabase)


def _resolve_school_id(supabase, settings: Settings) -> str:
    """Look up the school_id for the configured SCHOOL_CODE.

    Falls back to the seeded DPS East id if the lookup fails — useful
    for local dev where the user hasn't run the seed yet. Wrapped in
    try/except so RLS blocks surface as the fallback rather than 500.
    """
    try:
        row = (
            supabase.table("schools")
            .select("id")
            .eq("code", settings.SCHOOL_CODE)
            .maybe_single()
            .execute()
            .data
        )
        if row:
            return row["id"]
    except Exception:
        pass
    return "00000000-0000-0000-0000-000000000001"
