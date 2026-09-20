"""Driver position ingest endpoint.

Service-role only — NOT exposed to mobile clients. The real driver app
will POST here with a driver JWT; for MVP the simulator writes directly
to the DB and skips this route entirely.

Auth: an `X-Position-Secret` header that matches the `POSITION_SECRET`
env var. Distinct from `SUPABASE_SERVICE_ROLE_KEY` so a leaked bearer
token can't bypass the route's ACL.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, List

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .deps import CurrentUser, get_current_user, get_supabase_admin
from .notification_engine import emit_stop_transition

router = APIRouter()

def _get_data(res):
    if res is None:
        return None
    return getattr(res, "data", None)


def _load_bus_metadata(supabase, bus_id: str) -> tuple[str, list[dict[str, object]]] | None:
    """Return the bus number + schedule so live position updates can
    detect a stop transition without touching the route handler.
    """
    try:
        row = (
            supabase.table("buses")
            .select("id, number, schedule")
            .eq("id", bus_id)
            .maybe_single()
            .execute()
            .data
        )
    except Exception:
        return None
    if not row:
        return None
    return row.get("number", ""), row.get("schedule") or []


def _nearest_stop_index(schedule: list[dict[str, object]], latitude: float, longitude: float):
    """Find the nearest stop for a coordinate. Returns (index, stop_name).
    """
    best_idx = None
    best_name = None
    best_distance = None

    for idx, stop in enumerate(schedule):
        stop_lat = float(stop.get("latitude", 0))
        stop_lng = float(stop.get("longitude", 0))
        distance = (latitude - stop_lat) ** 2 + (longitude - stop_lng) ** 2
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_idx = idx
            best_name = str(stop.get("name", ""))

    if best_idx is None:
        return None
    return best_idx, best_name


def _maybe_emit_stop_transition(
    supabase,
    bus_id: str,
    latitude: float,
    longitude: float,
    previous_position: dict[str, object] | None,
):
    """Blogically evaluate whether the bus crossed a stop boundary.

    We avoid spamming alerts on every heartbeat by comparing the
    previous nearest stop against the current nearest stop. If the
    route has not crossed a stop boundary, no notification rows are
    written.
    """
    metadata = _load_bus_metadata(supabase, bus_id)
    if metadata is None:
        return

    bus_number, schedule = metadata
    if not schedule:
        return

    current = _nearest_stop_index(schedule, latitude, longitude)
    if current is None:
        return

    current_index, current_name = current

    if previous_position:
        prev_index = _nearest_stop_index(
            schedule,
            float(previous_position.get("latitude", latitude)),
            float(previous_position.get("longitude", longitude)),
        )
        if prev_index is None:
            return
        if prev_index[0] == current_index:
            return

    asyncio.run(
        emit_stop_transition(
            supabase,
            bus_id=bus_id,
            bus_number=bus_number,
            stop_index=current_index,
            stop_name=current_name,
        )
    )

class PositionIn(BaseModel):
    # Bounded so corrupted positions can't escape into the map.
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)

class PositionOut(BaseModel):
    bus_id: str
    latitude: float
    longitude: float
    speed: float = 0
    altitude: float = 0
    updated_at: str | None = None

@router.get("", response_model=List[PositionOut])
def list_positions(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
):
    """Return latest positions for all buses. Used for cold-start seeding in UI."""
    res = supabase.table("bus_positions").select("*").execute()
    return _get_data(res) or []

def _require_position_secret(
    x_position_secret: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    """Verify the caller knows the shared secret.

    In production this will be replaced by a per-driver JWT. For MVP
    we use a static shared secret so the simulator (and any future
    driver app) can authenticate without exposing the service-role key.
    """
    expected = (settings.POSITION_SECRET or "").strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Position ingest is not configured (POSITION_SECRET missing).",
        )
    if not x_position_secret or x_position_secret != expected:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Forbidden.")

@router.post("/{bus_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def upsert_position(
    bus_id: str,
    body: PositionIn,
    _=Depends(_require_position_secret),
    supabase=Depends(get_supabase_admin),
):
    previous_position = _get_data(
        supabase.table("bus_positions")
        .select("latitude, longitude")
        .eq("bus_id", bus_id)
        .maybe_single()
        .execute()
    )

    supabase.table("bus_positions").upsert({
        "bus_id": bus_id,
        "latitude": body.latitude,
        "longitude": body.longitude,
    }).execute()

    _maybe_emit_stop_transition(
        supabase,
        bus_id,
        body.latitude,
        body.longitude,
        previous_position,
    )
