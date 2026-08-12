"""Driver position ingest endpoint.

Service-role only — NOT exposed to mobile clients. The real driver app
will POST here with a driver JWT; for MVP the simulator writes directly
to the DB and skips this route entirely.

Auth: an `X-Position-Secret` header that matches the `POSITION_SECRET`
env var. Distinct from `SUPABASE_SERVICE_ROLE_KEY` so a leaked bearer
token can't bypass the route's ACL.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .deps import get_supabase_admin

router = APIRouter()


class PositionIn(BaseModel):
    # Bounded so corrupted positions can't escape into the map.
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


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
    supabase.table("bus_positions").upsert({
        "bus_id": bus_id,
        "latitude": body.latitude,
        "longitude": body.longitude,
    }).execute()