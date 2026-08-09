"""FastAPI dependencies: Supabase clients and the current-user resolver."""

from __future__ import annotations

from typing import Annotated, Any, Optional

from fastapi import Depends, Header, HTTPException, status
from jose import JWTError, jwt
from pydantic import BaseModel

from .config import Settings, get_settings


class CurrentUser(BaseModel):
    """Resolved identity injected into every protected route."""

    user_id: str
    role: str
    email: Optional[str] = None
    school_id: str = "00000000-0000-0000-0000-000000000001"


# ---------------------------------------------------------------------------
# Supabase client factories
# ---------------------------------------------------------------------------


def get_supabase_user(settings: Annotated[Settings, Depends(get_settings)]):
    """Client scoped to the caller's JWT (RLS enforced).

    Returns a new client per request so the user's session is honored.
    """
    from supabase import create_client  # local import keeps tests lightweight

    return create_client(settings.SUPABASE_URL, settings.SUPABASE_ANON_KEY)


def get_supabase_admin(settings: Annotated[Settings, Depends(get_settings)]):
    """Service-role client. Bypasses RLS. Server-only."""
    from supabase import create_client

    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)


# ---------------------------------------------------------------------------
# JWT / current user
# ---------------------------------------------------------------------------


def _decode_jwt(token: str, settings: Settings) -> dict[str, Any]:
    try:
        return jwt.decode(
            token,
            settings.SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
        ) from exc


def get_current_user(
    authorization: Annotated[Optional[str], Header()] = None,
    settings: Annotated[Settings, Depends(get_settings)] = None,
) -> CurrentUser:
    """Validate the bearer JWT and return a CurrentUser.

    Failure modes: 401 when the header is missing, malformed, or the
    token's signature/exp fails verification. We deliberately do NOT
    load the profile row here — the route does that so a stale profile
    row doesn't break the auth path.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
        )
    token = authorization.split(" ", 1)[1].strip()
    claims = _decode_jwt(token, settings)
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing subject.",
        )
    return CurrentUser(
        user_id=sub,
        role=claims.get("role", "authenticated"),
        email=claims.get("email"),
    )
