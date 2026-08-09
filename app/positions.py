"""Driver position ingest endpoint.

Service-role only — NOT exposed to mobile clients. The real driver app
will POST here with a driver JWT; for MVP the simulator writes directly
to the DB and skips this route entirely.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel

from ..deps import get_supabase_admin

router = APIRouter()


class PositionIn(BaseModel):
    latitude: float
    longitude: float


def _require_service_role(authorization: Annotated[str | None, Header()] = None) -> None:
    """Lightweight check: require the SUPABASE_SERVICE_ROLE_KEY as bearer.

    Real driver apps will use a separate JWT validator; for the MVP
    simulator path, we trust the service role key directly. The route is
    not exposed to mobile clients via CORS (Render env doesn't include
    the mobile origin).
    """
    from ..config import get_settings

    expected = get_settings().SUPABASE_SERVICE_ROLE_KEY
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing token.")
    if authorization.split(" ", 1)[1].strip() != expected:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Forbidden.")


@router.post("/{bus_id}", status_code=status.HTTP_204_NO_CONTENT)
def upsert_position(
    bus_id: str,
    body: PositionIn,
    _=Depends(_require_service_role),
    supabase=Depends(get_supabase_admin),
) -> None:
    supabase.table("bus_positions").upsert({
        "bus_id": bus_id,
        "latitude": body.latitude,
        "longitude": body.longitude,
    }).execute()
