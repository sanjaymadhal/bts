"""Profiles endpoints.

MVP: only `GET /profiles?role=driver` (drivers picker on the bus sheet).
"""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..deps import CurrentUser, get_current_user, get_supabase_user

router = APIRouter()


class DriverOut(BaseModel):
    id: str
    display_name: str
    phone: str | None = None


@router.get("", response_model=list[DriverOut])
def list_profiles(
    role: Annotated[Optional[str], Query()] = None,
    _user: Annotated[CurrentUser, Depends(get_current_user)] = None,
    supabase=Depends(get_supabase_user),
) -> list[DriverOut]:
    q = supabase.table("profiles").select("id, role, display_name, phone")
    if role:
        q = q.eq("role", role)
    rows = q.execute().data or []
    return [
        DriverOut(
            id=r["id"],
            display_name=r["display_name"],
            phone=r.get("phone"),
        )
        for r in rows
    ]
