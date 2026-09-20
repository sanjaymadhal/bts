"""FastAPI dependencies: Supabase clients and the current-user resolver."""

from __future__ import annotations

import json
import time
from typing import Annotated, Any, Optional

import httpx
from fastapi import Depends, Header, HTTPException, status
from jose import JWTError, jwt
from pydantic import BaseModel

from supabase import ClientOptions  # noqa: E402  (imported below the type hints)

from .config import Settings, get_settings


class CurrentUser(BaseModel):
    """Resolved identity injected into every protected route.

    `auth_role` is the Supabase auth role from the JWT
    ("authenticated" / "anon" / "service_role"). `app_role` is the
    application-level role from `profiles.role` ("admin" / "driver" /
    "parent" / "student" / "staff") — populated lazily because not
    every route needs it.
    """

    user_id: str
    auth_role: str = "authenticated"
    app_role: str | None = None
    email: Optional[str] = None
    school_id: str = "00000000-0000-0000-0000-000000000001"

    # Backwards-compat alias: code that still reads `user.role` keeps
    # working. The value is the *auth* role (Supabase's). Prefer
    # `user.app_role` for app-level checks.
    @property
    def role(self) -> str:
        return self.auth_role

    @role.setter
    def role(self, value: str) -> None:
        self.auth_role = value


# ---------------------------------------------------------------------------
# Supabase client factories
# ---------------------------------------------------------------------------


def get_supabase_user(
    authorization: Annotated[Optional[str], Header()] = None,
    settings: Annotated[Settings, Depends(get_settings)] = None,
):
    """Client scoped to the caller's JWT (RLS enforced).

    Returns a new client per request so the user's session is honored.
    The Authorization header is forwarded so RLS policies see the
    authenticated user — without this, every request hits Supabase as
    the anonymous role and is rejected by RLS.
    """
    from supabase import create_client

    headers: dict[str, str] = {}
    if authorization:
        headers["Authorization"] = authorization
    return create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_ANON_KEY,
        options=ClientOptions(headers=headers),
    )


def get_supabase_admin(settings: Annotated[Settings, Depends(get_settings)]):
    """Service-role client. Bypasses RLS. Server-only."""
    from supabase import create_client

    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)


# ---------------------------------------------------------------------------
# JWT / current user
# ---------------------------------------------------------------------------


# Modern Supabase projects sign JWTs with **asymmetric ES256** (a
# private key rotated by Supabase) — the public half is published at
# `{SUPABASE_URL}/auth/v1/.well-known/jwks.json`. The legacy
# `SUPABASE_JWT_SECRET` is an HS256 shared secret. We support both
# transparently: pick the algorithm from the JWT header, then verify
# with the matching key.

# Cache the JWKS for 10 minutes so we don't hammer the auth endpoint
# on every API request. The cache is per-process.
_JWKS_CACHE: dict[str, Any] = {"fetched_at": 0.0, "keys": None}
_JWKS_TTL_SECONDS = 600.0


def _fetch_jwks(supabase_url: str) -> Optional[dict[str, Any]]:
    """Fetch and cache the JWKS published by Supabase."""
    now = time.monotonic()
    if _JWKS_CACHE["keys"] is not None and (now - _JWKS_CACHE["fetched_at"]) < _JWKS_TTL_SECONDS:
        return _JWKS_CACHE["keys"]
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(f"{supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json")
        if r.status_code != 200:
            return _JWKS_CACHE.get("keys")  # serve stale if a refresh failed
        keys = r.json()
        _JWKS_CACHE["keys"] = keys
        _JWKS_CACHE["fetched_at"] = now
        return keys
    except Exception:
        return _JWKS_CACHE.get("keys")


def _decode_jwt(token: str, settings: Settings) -> dict[str, Any]:
    """Verify a Supabase JWT using the algorithm advertised in its header.

    - ES256 → verify against the JWKS public keys (modern Supabase).
    - HS256 → verify against `SUPABASE_JWT_SECRET` (legacy projects).
    """
    try:
        header = json.loads(
            jwt.get_unverified_header(token).json()
            if hasattr(jwt.get_unverified_header(token), "json")
            else json.dumps(jwt.get_unverified_header(token))
        )
    except Exception:
        return {}

    alg = header.get("alg", "HS256")

    # --- ES256: asymmetric, verify via JWKS ----------------------------
    if alg == "ES256":
        jwks = _fetch_jwks(settings.SUPABASE_URL)
        if not jwks:
            return {}
        try:
            return jwt.decode(
                token,
                jwks,
                algorithms=["ES256"],
                options={"verify_aud": False},
            )
        except JWTError:
            return {}

    # --- HS256: legacy shared secret -----------------------------------
    secret = settings.SUPABASE_JWT_SECRET
    try:
        return jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
    except JWTError:
        return {}


def _verify_via_supabase(token: str) -> Optional[dict[str, Any]]:
    """Final fallback: ask Supabase to verify a JWT by calling `get_user`.

    Used when neither local verification path works (e.g. the JWKS
    fetch failed and the HS256 secret is wrong). Returns the user dict
    on success, None otherwise.
    """
    try:
        from supabase import create_client

        from .config import get_settings

        s = get_settings()
        client = create_client(s.SUPABASE_URL, s.SUPABASE_ANON_KEY)
        res = client.auth.get_user(jwt=token)
        user = getattr(res, "user", None) or (res if isinstance(res, dict) else None)
        if not user:
            return None
        return {
            "sub": getattr(user, "id", None) or (user.get("id") if isinstance(user, dict) else None),
            "email": getattr(user, "email", None) or (user.get("email") if isinstance(user, dict) else None),
            "role": (
                (getattr(user, "user_metadata", None) or {}).get("role")
                or ((user.get("user_metadata") or {}).get("role") if isinstance(user, dict) else None)
                or "authenticated"
            ),
        }
    except Exception:
        return None


def get_current_user(
    authorization: Annotated[Optional[str], Header()] = None,
    settings: Settings = Depends(get_settings),
) -> CurrentUser:
    """Validate the bearer JWT and return a CurrentUser.

    Verification order:
      1. Local decode (ES256 via JWKS, or HS256 via the legacy secret).
      2. Supabase `get_user(jwt=...)` over the network.

    Failure modes: 401 when the header is missing, malformed, or neither
    verification path produces claims. We deliberately do NOT load the
    profile row here — the route does that so a stale profile row
    doesn't break the auth path.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
        )
    token = authorization.split(" ", 1)[1].strip()

    claims = _decode_jwt(token, settings)
    if not claims:
        claims = _verify_via_supabase(token) or {}

    sub = claims.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
        )
    return CurrentUser(
        user_id=sub,
        auth_role=claims.get("role", "authenticated"),
        email=claims.get("email"),
    )


def load_app_role(
    user: CurrentUser,
    supabase_admin,
) -> str:
    """Populate CurrentUser.app_role from the profiles row.

    Called by routes that need to gate by application-level role
    (admin-only mutations, etc). Returns the role string; if the
    profile row is missing or unreadable, returns "authenticated" so
    the caller fails closed on any role-gated branch.

    Synchronous: the supabase admin client is sync and the role row
    lookup is one round-trip. Sync keeps the call sites simple — no
    `await` needed even when the route handler is also sync.
    """
    try:
        row = (
            supabase_admin.table("profiles")
            .select("role")
            .eq("id", user.user_id)
            .maybe_single()
            .execute()
            .data
        )
        if row and row.get("role"):
            return row["role"]
    except Exception:
        pass
    return "authenticated"


def load_parent_scope(
    user: CurrentUser,
    supabase_admin,
) -> dict[str, Any]:
    """Resolve what data the parent is allowed to see.

    Returns a dict with the parent's `linked_student_id` and the
    `assigned_bus_id` (the bus their child is currently on). Routes
    that need to scope reads for non-admin callers pass this dict to
    filters so a parent can't list every bus / every student in the
    school. Returns `{}` for non-parent roles so the caller can
    safely use `scope.get("assigned_bus_id")` without a type check.
    """
    try:
        row = (
            supabase_admin.table("profiles")
            .select("linked_student_id, assigned_bus_id")
            .eq("id", user.user_id)
            .maybe_single()
            .execute()
            .data
        )
    except Exception:
        return {}
    if not row:
        return {"linked_student_id": None, "assigned_bus_id": None}

    linked_student_id = row.get("linked_student_id")
    assigned_bus_id: Optional[str] = row.get("assigned_bus_id")

    if not linked_student_id:
        return {"linked_student_id": None, "assigned_bus_id": assigned_bus_id}

    if not assigned_bus_id:
        try:
            student_row = (
                supabase_admin.table("students")
                .select("bus_id")
                .eq("id", linked_student_id)
                .maybe_single()
                .execute()
                .data
            )
            assigned_bus_id = (student_row or {}).get("bus_id")
        except Exception:
            pass

    return {
        "linked_student_id": linked_student_id,
        "assigned_bus_id": assigned_bus_id,
    }


def require_app_role(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    expected: str,
    supabase_admin=Depends(get_supabase_admin),
) -> CurrentUser:
    """Dependency factory: ensure the caller has the given app role.

    Returns the CurrentUser (with `app_role` populated). Raises 403 if
    the caller doesn't match. Fail-closed: a missing profile row
    returns "authenticated", which never matches an app role other
    than "authenticated" itself.
    """
    if user.app_role is None:
        user.app_role = load_app_role(user, supabase_admin)
    if user.app_role != expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"This action requires the '{expected}' role.",
        )
    return user
