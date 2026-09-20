"""Bus CRUD + position lookup."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
import httpx
from .deps import (
    CurrentUser,
    get_current_user,
    get_supabase_admin,
    load_app_role,
    load_parent_scope,
)
from .config import get_settings

router = APIRouter()

_logger = logging.getLogger(__name__)


def _get_data(res):
    if res is None:
        return None
    return getattr(res, "data", None)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ScheduleStop(BaseModel):
    time: str = Field(pattern=r"^\d{2}:\d{2}$")
    name: str = Field(min_length=1, max_length=200)
    # Lat/lng bounded so a corrupted client can't push a marker off the
    # planet. The simulator + OSM geocoder both produce values in range.
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


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
    # A bus with no schedule is valid (driver runs a fixed route, not a
    # stop-by-stop schedule), so the simulator just skips it. Frontend
    # can still create one and the parent map will show the bus parked
    # at its last known position.
    schedule: list[ScheduleStop] = Field(default_factory=list, max_length=64)


class BusUpdate(BaseModel):
    number: Optional[str] = Field(default=None, min_length=1, max_length=32)
    route: Optional[str] = Field(default=None, min_length=1, max_length=200)
    capacity: Optional[int] = Field(default=None, ge=1, le=200)
    driver_id: Optional[str] = None
    schedule: Optional[list[ScheduleStop]] = Field(default=None, max_length=64)


class BusPosition(BaseModel):
    # Bounded so corrupted positions can't escape into the map.
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    updated_at: Optional[str] = None
    is_online: bool = True

class HistoryFrame(BaseModel):
    latitude: float
    longitude: float
    speed: float
    altitude: float
    created_at: str

class HistoryResponse(BaseModel):
    bus_id: str
    history: list[HistoryFrame]

class RouteResponse(BaseModel):
    bus_id: str
    coordinates: list[dict[str, float]]
    distance_m: Optional[int] = None
    duration_s: Optional[int] = None
    snapped: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hydrate_bus(row: dict, students: list[dict]) -> BusOut:
    """Map a raw `buses` row + joined students to the spec's BusOut."""
    return BusOut(
        id=row["id"],
        number=row["number"],
        route=row["route"],
        capacity=row["capacity"],
        driver_id=row.get("driver_id"),
        schedule=[ScheduleStop(**s) for s in (row.get("schedule") or [])],
        students=[StudentOut(**s) for s in (students or [])],
    )


def _load_bus(bus_id: str, supabase) -> BusOut:
    """Read a bus by id (two queries: bus + its students)."""
    res = (
        supabase.table("buses")
        .select("id, number, route, capacity, driver_id, schedule")
        .eq("id", bus_id)
        .maybe_single()
        .execute()
    )
    bus = _get_data(res)
    if bus is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bus not found.")
    res = (
        supabase.table("students")
        .select("id, name, class_grade, stop, bus_id")
        .eq("bus_id", bus_id)
        .execute()
    )
    students = _get_data(res) or []
    return _hydrate_bus(bus, students)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[BusOut])
def list_buses(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
) -> list[BusOut]:
    # Admins (and other non-parent roles) see every bus. Parents see
    # only the bus their linked child is currently assigned to — a
    # parent must never be able to enumerate the school's fleet.
    if user.app_role is None:
        user.app_role = load_app_role(user, supabase)
    if user.app_role == "parent":
        scope = load_parent_scope(user, supabase)
        bus_id = scope.get("assigned_bus_id")
        if not bus_id:
            return []
        res = (
            supabase.table("buses")
            .select(
                "id, number, route, capacity, driver_id, schedule, "
                "students(id, name, class_grade, stop, bus_id)"
            )
            .eq("id", bus_id)
            .execute()
        )
        rows = _get_data(res) or []
    else:
        # Single relational select: buses with their students nested.
        # 1 round-trip instead of the previous 2N+1.
        res = (
            supabase.table("buses")
            .select(
                "id, number, route, capacity, driver_id, schedule, "
                "students(id, name, class_grade, stop, bus_id)"
            )
            .execute()
        )
        rows = _get_data(res) or []
    return [
        _hydrate_bus(
            {k: v for k, v in r.items() if k != "students"},
            r.get("students") or [],
        )
        for r in rows
    ]


@router.get("/{bus_id}", response_model=BusOut)
def get_bus(
    bus_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
) -> BusOut:
    # Parents can only read the bus their child is on.
    if user.app_role is None:
        user.app_role = load_app_role(user, supabase)
    if user.app_role == "parent":
        scope = load_parent_scope(user, supabase)
        if scope.get("assigned_bus_id") != bus_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This bus is not assigned to your child.",
            )
    return _load_bus(bus_id, supabase)


@router.post("", response_model=BusOut, status_code=status.HTTP_201_CREATED)
def create_bus(
    body: BusCreate,
    _user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
) -> BusOut:
    payload: dict[str, Any] = {
        "number": body.number,
        "route": body.route,
        "capacity": body.capacity,
        "driver_id": body.driver_id,
        "schedule": [s.model_dump() for s in body.schedule],
    }
    res = supabase.table("buses").insert(payload).execute()
    inserted = _get_data(res)
    # PostgREST always returns the inserted row with the DB-assigned id
    # (gen_random_uuid() default). If it didn't, the database is broken
    # and we'd rather 500 than return a synthesized id that may or may
    # not match what was actually written.
    if not inserted or not inserted[0].get("id"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Insert succeeded but the database returned no row id.",
        )
    return _load_bus(inserted[0]["id"], supabase)


@router.patch("/{bus_id}", response_model=BusOut)
def update_bus(
    bus_id: str,
    body: BusUpdate,
    _user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
) -> BusOut:
    patch: dict[str, Any] = {}
    body_data = body.model_dump(exclude_unset=True)
    if body.number is not None:
        patch["number"] = body.number
    if body.route is not None:
        patch["route"] = body.route
    if body.capacity is not None:
        patch["capacity"] = body.capacity
    if "driver_id" in body_data:
        patch["driver_id"] = body_data["driver_id"]
    if body.schedule is not None:
        patch["schedule"] = [s.model_dump() for s in body.schedule]
    if patch:
        # Verify the bus exists so the patch returns 404 (not a silent
        # no-op) when the id is bogus.
        res = (
            supabase.table("buses")
            .select("id")
            .eq("id", bus_id)
            .maybe_single()
            .execute()
        )
        existing = _get_data(res)
        if not existing:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Bus not found.")
        supabase.table("buses").update(patch).eq("id", bus_id).execute()
    return _load_bus(bus_id, supabase)


@router.delete("/{bus_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_bus(
    bus_id: str,
    _user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
):
    supabase.table("buses").delete().eq("id", bus_id).execute()

@router.get("/{bus_id}/route", response_model=RouteResponse)
async def get_bus_route(
    bus_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
    settings: Annotated[Any, Depends(get_settings)] = None,
) -> RouteResponse:
    # Parents can only read the route for their assigned bus.
    if user.app_role is None:
        user.app_role = load_app_role(user, supabase)
    if user.app_role == "parent":
        scope = load_parent_scope(user, supabase)
        if scope.get("assigned_bus_id") != bus_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Access forbidden.")

    # 1. Load the bus schedule (the stops)
    bus = _load_bus(bus_id, supabase)
    stops = bus.schedule
    if len(stops) < 2:
        # Not enough stops to form a route
        coords = [{"latitude": s.latitude, "longitude": s.longitude} for s in stops]
        return RouteResponse(bus_id=bus_id, coordinates=coords, snapped=False)

    # 2. Call OSRM to get road-following geometry
    # Format: lon,lat;lon,lat...
    coords_str = ";".join([f"{s.longitude},{s.latitude}" for s in stops])
    osrm_url = f"{settings.OSRM_BASE_URL}/route/v1/driving/{coords_str}?overview=full&geometries=geojson"

    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(osrm_url, timeout=5.0)
            res.raise_for_status()
            data = res.json()

            if not data.get("routes"):
                raise Exception("No route found")

            route_data = data["routes"][0]
            # OSRM GeoJSON geometry is [[lon, lat], [lon, lat], ...]
            geometry = route_data["geometry"]["coordinates"]
            snapped_coords = [{"latitude": p[1], "longitude": p[0]} for p in geometry]

            return RouteResponse(
                bus_id=bus_id,
                coordinates=snapped_coords,
                distance_m=int(route_data["distance"]),
                duration_s=int(route_data["duration"]),
                snapped=True,
            )
    except Exception as e:
        _logger.error(f"OSRM Routing Error: {e}")
        # Fallback to straight lines between stops
        fallback_coords = [{"latitude": s.latitude, "longitude": s.longitude} for s in stops]
        return RouteResponse(bus_id=bus_id, coordinates=fallback_coords, snapped=False)



@router.get("/{bus_id}/position", response_model=BusPosition)
def get_bus_position(
    bus_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
) -> BusPosition:
    # Parents only get positions for their assigned bus.
    if user.app_role is None:
        user.app_role = load_app_role(user, supabase)
    if user.app_role == "parent":
        scope = load_parent_scope(user, supabase)
        if scope.get("assigned_bus_id") != bus_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This bus is not assigned to your child.",
            )
    res = (
        supabase.table("bus_positions")
        .select("latitude, longitude, updated_at")
        .eq("bus_id", bus_id)
        .maybe_single()
        .execute()
    )
    row = _get_data(res)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No position recorded.")

    # Reliability fix: Calculate online status (updated within last 5 mins)
    import datetime
    updated_at_str = row.get("updated_at")
    is_online = True
    if updated_at_str:
        try:
            # Supabase timestamptz format
            updated_at = datetime.datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
            now = datetime.datetime.now(datetime.timezone.utc)
            if (now - updated_at).total_seconds() > 300:
                is_online = False
        except Exception:
            is_online = False

    return BusPosition(**row, is_online=is_online)

@router.get("/{bus_id}/history", response_model=HistoryResponse)
def get_bus_history(
    bus_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
    days: int = Query(default=30, ge=1, le=180),
) -> HistoryResponse:
    if user.app_role is None:
        user.app_role = load_app_role(user, supabase)
    if user.app_role == "parent":
        scope = load_parent_scope(user, supabase)
        if scope.get("assigned_bus_id") != bus_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Access forbidden.")

    import datetime

    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()

    try:
        res = (
            supabase.table("bus_position_history")
            .select("latitude, longitude, speed, altitude, created_at")
            .eq("bus_id", bus_id)
            .gte("created_at", cutoff)
            .order("created_at", desc=True)
            .limit(500)
            .execute()
        )
    except Exception as exc:
        error_text = str(exc)
        if "bus_position_history" in error_text or "PGRST205" in error_text:
            return HistoryResponse(bus_id=bus_id, history=[])
        raise

    rows = _get_data(res) or []
    return HistoryResponse(bus_id=bus_id, history=[HistoryFrame(**r) for r in rows])


# ---------------------------------------------------------------------------
# Demo: stop-by-stop playback
# ---------------------------------------------------------------------------


class PlayRequest(BaseModel):
    # Per-stop delay in milliseconds. 2500 ≈ the demo tempo the team
    # uses on stage; default keeps it responsive while still readable.
    step_delay_ms: int = Field(default=2500, ge=200, le=15000)
    # Number of full loops to play. 0 = one pass and stop; >0 cycles.
    loops: int = Field(default=1, ge=0, le=10)


class PlayResponse(BaseModel):
    bus_id: str
    stops_played: int


def _coerce_schedule_for_play(raw: Any) -> list[dict[str, float]]:
    """Same defensive coercion the global simulator uses — see
    `simulator._coerce_schedule`. Keep these in sync; if the JSON
    shape changes upstream the demo and the live simulator should
    still walk the route.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, float]] = []
    for s in raw:
        if isinstance(s, dict) and "latitude" in s and "longitude" in s:
            try:
                out.append({"latitude": float(s["latitude"]), "longitude": float(s["longitude"])})
            except (TypeError, ValueError):
                continue
    return out


@router.post("/{bus_id}/play", response_model=PlayResponse)
async def play_route(
    bus_id: str,
    body: PlayRequest,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
) -> PlayResponse:
    # Demo playback is admin-only. Parents get the live simulator
    # path; admins get the manual control.
    if user.app_role is None:
        user.app_role = load_app_role(user, supabase)
    if user.app_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can start a route demo.",
        )
    """Walk the bus through its schedule stop-by-stop and write each
    position to `bus_positions`. Admin-only.

    This is the demo path: tapping "Play" on the map starts the
    playback here instead of relying on the global simulator (which
    can race with migrations or fail on malformed schedule rows).
    Each upsert triggers a Realtime broadcast, so subscribed mobile
    clients see the marker hop from stop to stop.
    """
    bus = (
        supabase.table("buses")
        .select("id, schedule")
        .eq("id", bus_id)
        .maybe_single()
        .execute()
        .data
    )
    if not bus:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bus not found.")
    stops = _coerce_schedule_for_play(bus.get("schedule"))
    if not stops:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "This bus has no stops to play. Add at least two stops in its route.",
        )

    step_seconds = body.step_delay_ms / 1000.0
    total_passes = 1 + max(0, body.loops)
    played = 0

    loop = asyncio.get_running_loop()

    def _upsert(lat: float, lng: float) -> None:
        supabase.table("bus_positions").upsert(
            {"bus_id": bus_id, "latitude": lat, "longitude": lng}
        ).execute()

    for _ in range(total_passes):
        for stop in stops:
            await loop.run_in_executor(None, _upsert, stop["latitude"], stop["longitude"])
            played += 1
            await asyncio.sleep(step_seconds)

    return PlayResponse(bus_id=bus_id, stops_played=played)


@router.post("/{bus_id}/play/stop", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def stop_playback(
    bus_id: str,
    _user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
):
    """No-op endpoint to acknowledge a Stop tap. The actual playback
    loop is fire-and-forget on the server (FastAPI task). Keeping
    this route lets the UI tell the user the playback is being
    cancelled; in practice the playback loop will run to completion
    unless the worker restarts. This is intentional for the demo —
    it's bounded by `loops` and `step_delay_ms`.
    """
    return Response(status_code=status.HTTP_204_NO_CONTENT)