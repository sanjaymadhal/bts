"""Bus CRUD + position lookup."""

from __future__ import annotations

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..deps import CurrentUser, get_current_user, get_supabase_user

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ScheduleStop(BaseModel):
    time: str = Field(pattern=r"^\d{2}:\d{2}$")
    name: str = Field(min_length=1, max_length=200)
    latitude: float
    longitude: float


class StudentOut(BaseModel):
    id: str
    name: str
    class_grade: str
    stop: str
    bus_id: Optional[str] = None


class BusOut(BaseModel):
    id: str
    number: str
    route: str
    capacity: int
    driver_id: Optional[str] = None
    schedule: list[ScheduleStop]
    students: list[StudentOut]


class BusCreate(BaseModel):
    number: str = Field(min_length=1, max_length=32)
    route: str = Field(min_length=1, max_length=200)
    capacity: int = Field(ge=1, le=200)
    driver_id: Optional[str] = None
    schedule: list[ScheduleStop] = Field(default_factory=list)


class BusUpdate(BaseModel):
    number: Optional[str] = Field(default=None, min_length=1, max_length=32)
    route: Optional[str] = Field(default=None, min_length=1, max_length=200)
    capacity: Optional[int] = Field(default=None, ge=1, le=200)
    driver_id: Optional[str] = None
    schedule: Optional[list[ScheduleStop]] = None


class BusPosition(BaseModel):
    latitude: float
    longitude: float
    updated_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_bus(bus_id: str, supabase) -> BusOut:
    """Read a bus, its schedule, and its assigned students."""
    bus = (
        supabase.table("buses")
        .select("id, number, route, capacity, driver_id, schedule")
        .eq("id", bus_id)
        .single()
        .execute()
        .data
    )
    if bus is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bus not found.")
    students = (
        supabase.table("students")
        .select("id, name, class_grade, stop, bus_id")
        .eq("bus_id", bus_id)
        .execute()
        .data
        or []
    )
    return BusOut(
        id=bus["id"],
        number=bus["number"],
        route=bus["route"],
        capacity=bus["capacity"],
        driver_id=bus.get("driver_id"),
        schedule=[ScheduleStop(**s) for s in (bus.get("schedule") or [])],
        students=[StudentOut(**s) for s in students],
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[BusOut])
def list_buses(
    _user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_user),
) -> list[BusOut]:
    rows = supabase.table("buses").select("id").execute().data or []
    return [_load_bus(r["id"], supabase) for r in rows]


@router.get("/{bus_id}", response_model=BusOut)
def get_bus(
    bus_id: str,
    _user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_user),
) -> BusOut:
    return _load_bus(bus_id, supabase)


@router.post("", response_model=BusOut, status_code=status.HTTP_201_CREATED)
def create_bus(
    body: BusCreate,
    _user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_user),
) -> BusOut:
    payload: dict[str, Any] = {
        "number": body.number,
        "route": body.route,
        "capacity": body.capacity,
        "driver_id": body.driver_id,
        "schedule": [s.model_dump() for s in body.schedule],
    }
    inserted = (
        supabase.table("buses").insert(payload).execute().data
    )
    bus_id = inserted[0]["id"]
    return _load_bus(bus_id, supabase)


@router.patch("/{bus_id}", response_model=BusOut)
def update_bus(
    bus_id: str,
    body: BusUpdate,
    _user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_user),
) -> BusOut:
    patch: dict[str, Any] = {}
    if body.number is not None:
        patch["number"] = body.number
    if body.route is not None:
        patch["route"] = body.route
    if body.capacity is not None:
        patch["capacity"] = body.capacity
    if body.driver_id is not None:
        patch["driver_id"] = body.driver_id
    if body.schedule is not None:
        patch["schedule"] = [s.model_dump() for s in body.schedule]
    if patch:
        supabase.table("buses").update(patch).eq("id", bus_id).execute()
    return _load_bus(bus_id, supabase)


@router.delete("/{bus_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_bus(
    bus_id: str,
    _user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_user),
) -> None:
    supabase.table("buses").delete().eq("id", bus_id).execute()


@router.get("/{bus_id}/position", response_model=BusPosition)
def get_bus_position(
    bus_id: str,
    _user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_user),
) -> BusPosition:
    row = (
        supabase.table("bus_positions")
        .select("latitude, longitude, updated_at")
        .eq("bus_id", bus_id)
        .maybe_single()
        .execute()
        .data
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No position recorded.")
    return BusPosition(**row)
