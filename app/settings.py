"""School-scoped settings.

Single row per school. PATCH is partial — only present fields are
written. The frontend uses this for school_name, auto_assign_stops,
language, notifications_enabled, and default_alert_radius_m.
"""

from __future__ import annotations

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..deps import CurrentUser, get_current_user, get_supabase_user

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load(school_id: str, supabase) -> SchoolSettingsOut:
    settings_row = (
        supabase.table("school_settings")
        .select("school_id, notifications_enabled, auto_assign_stops, language, default_alert_radius_m")
        .eq("school_id", school_id)
        .single()
        .execute()
        .data
    )
    school_row = (
        supabase.table("schools")
        .select("id, name, code")
        .eq("id", school_id)
        .single()
        .execute()
        .data
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
    supabase=Depends(get_supabase_user),
    settings: Settings = Depends(get_settings),
) -> SchoolSettingsOut:
    school_id = _resolve_school_id(supabase, settings)
    return _load(school_id, supabase)


@router.patch("", response_model=SchoolSettingsOut)
def patch_settings(
    body: SchoolSettingsPatch,
    _user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_user),
    settings: Settings = Depends(get_settings),
) -> SchoolSettingsOut:
    school_id = _resolve_school_id(supabase, settings)

    # school_name lives on `schools`, not `school_settings`.
    if body.school_name is not None:
        supabase.table("schools").update({"name": body.school_name}).eq("id", school_id).execute()

    settings_patch: dict[str, Any] = {
        k: v
        for k, v in body.model_dump(exclude_none=True).items()
        if k != "school_name"
    }
    if settings_patch:
        supabase.table("school_settings").update(settings_patch).eq("school_id", school_id).execute()
    return _load(school_id, supabase)


def _resolve_school_id(supabase, settings: Settings) -> str:
    """Look up the school_id for the configured SCHOOL_CODE.

    Falls back to the seeded DPS East id if the lookup fails — useful
    for local dev where the user hasn't run the seed yet.
    """
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
    return "00000000-0000-0000-0000-000000000001"
