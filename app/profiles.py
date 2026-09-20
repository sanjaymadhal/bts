"""Profiles endpoints.

Used by the admin app's driver picker on the bus sheet, and by
the parent app's profile lookups.
"""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from .config import Settings, get_settings
from .deps import (
    CurrentUser,
    get_current_user,
    get_supabase_admin,
    load_app_role,
    load_parent_scope,
)

router = APIRouter()


_VALID_ROLES = {"admin", "driver", "staff", "parent", "student"}


class DriverOut(BaseModel):
    id: str
    display_name: str
    phone: str | None = None


@router.get("", response_model=list[DriverOut])
def list_profiles(
    role: Annotated[Optional[str], Query(pattern=r"^(admin|driver|staff|parent|student)$")] = None,
    user: Annotated[CurrentUser, Depends(get_current_user)] = None,
    supabase=Depends(get_supabase_admin),
    settings: Settings = Depends(get_settings),
) -> list[DriverOut]:
    # Validate role explicitly (Query's `pattern` catches well-formed
    # values but not None / blank — keep this for safety).
    if role is not None and role not in _VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown role '{role}'.",
        )

    # Parents must not be able to enumerate the school's profile
    # directory. Allow only the bus's driver + themselves; deny
    # everything else.
    if user.app_role is None:
        user.app_role = load_app_role(user, supabase)
    if user.app_role == "parent":
        scope = load_parent_scope(user, supabase)
        bus_id = scope.get("assigned_bus_id")
        driver_id: Optional[str] = None
        if bus_id:
            try:
                bus_row = (
                    supabase.table("buses")
                    .select("driver_id")
                    .eq("id", bus_id)
                    .maybe_single()
                    .execute()
                    .data
                )
                driver_id = (bus_row or {}).get("driver_id")
            except Exception:
                driver_id = None
        if not driver_id:
            return []
        # Just their own + their bus's driver.
        q = (
            supabase.table("profiles")
            .select("id, role, display_name, phone")
            .eq("school_id", "00000000-0000-0000-0000-000000000001")
            .in_("id", [user.user_id, driver_id])
        )
        rows = getattr(q.execute(), "data", None) or []
        return [
            DriverOut(
                id=r["id"],
                display_name=r["display_name"],
                phone=r.get("phone"),
            )
            for r in rows
            if r.get("role") == (role or r.get("role"))
        ]

    # Resolve the school id for the configured SCHOOL_CODE so we
    # never leak profiles from a different school in multi-tenant
    # databases. Same fallback as settings.py.
    school_id: Optional[str] = None
    try:
        school_row = (
            supabase.table("schools")
            .select("id")
            .eq("code", settings.SCHOOL_CODE)
            .maybe_single()
            .execute()
            .data
        )
        if school_row:
            school_id = school_row["id"]
    except Exception:
        school_id = None
    if not school_id:
        school_id = "00000000-0000-0000-0000-000000000001"

    q = supabase.table("profiles").select("id, role, display_name, phone").eq("school_id", school_id)
    if role:
        q = q.eq("role", role)
    res = q.execute()
    rows = getattr(res, "data", None) or []
    return [
        DriverOut(
            id=r["id"],
            display_name=r["display_name"],
            phone=r.get("phone"),
        )
        for r in rows
    ]