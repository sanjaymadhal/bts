"""Auth endpoints: login, reset-password, get-me, update-me.

The route layer is thin: it validates inputs, calls the Supabase client,
and maps rows to the spec's response shape. DB errors bubble up as 500;
4xx is reserved for "client gave us something we can't act on."
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from ..deps import CurrentUser, get_current_user, get_supabase_user

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class Profile(BaseModel):
    id: str
    role: str
    school_id: str
    school_name: str
    display_name: str
    phone: str | None = None
    linked_student_id: str | None = None
    email: str | None = None


class LoginResponse(BaseModel):
    access_token: str
    profile: Profile


class ResetRequest(BaseModel):
    email: EmailStr


class ResetResponse(BaseModel):
    ok: bool = True


class UpdateMeRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    phone: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_profile(user: CurrentUser, supabase) -> Profile:
    """Read the profile row + the joined school name."""
    rows = (
        supabase.table("profiles")
        .select("id, role, school_id, display_name, phone, linked_student_id")
        .eq("id", user.user_id)
        .single()
        .execute()
        .data
    )
    if rows is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Profile not found.",
        )
    school_name = (
        supabase.table("schools")
        .select("name")
        .eq("id", rows["school_id"])
        .single()
        .execute()
        .data.get("name", "")
    )
    return Profile(
        id=rows["id"],
        role=rows["role"],
        school_id=rows["school_id"],
        school_name=school_name,
        display_name=rows["display_name"],
        phone=rows.get("phone"),
        linked_student_id=rows.get("linked_student_id"),
        email=user.email,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/login", response_model=LoginResponse)
def login(
    body: LoginRequest,
    supabase=Depends(get_supabase_user),
) -> LoginResponse:
    """Exchange email+password for a Supabase JWT + profile."""
    try:
        auth = supabase.auth.sign_in_with_password(
            {"email": body.email, "password": body.password}
        )
    except Exception as exc:  # supabase-py raises a generic exception on bad creds
        # Don't surface the underlying error message — that would let
        # attackers enumerate which emails exist.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        ) from exc

    if not auth.session or not auth.session.access_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    user_meta = auth.user.user_metadata or {}
    user = CurrentUser(
        user_id=auth.user.id,
        role=(user_meta.get("role") or "authenticated"),
        email=auth.user.email,
    )
    profile = _load_profile(user, supabase)
    return LoginResponse(access_token=auth.session.access_token, profile=profile)


@router.post("/reset-password", response_model=ResetResponse)
def reset_password(
    body: ResetRequest,
    supabase=Depends(get_supabase_user),
) -> ResetResponse:
    """Trigger a Supabase reset email. Always 200 — no enumeration."""
    try:
        supabase.auth.reset_password_for_email(body.email)
    except Exception:
        pass
    return ResetResponse()


@router.get("/me", response_model=Profile)
def get_me(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_user),
) -> Profile:
    return _load_profile(user, supabase)


@router.patch("/me", response_model=Profile)
def update_me(
    body: UpdateMeRequest,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_user),
) -> Profile:
    patch: dict[str, Any] = {}
    if body.display_name is not None:
        patch["display_name"] = body.display_name
    if body.phone is not None:
        patch["phone"] = body.phone
    if patch:
        (
            supabase.table("profiles")
            .update(patch)
            .eq("id", user.user_id)
            .execute()
        )
    return _load_profile(user, supabase)
