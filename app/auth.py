"""Auth endpoints: login, reset-password, get-me, update-me.

The route layer is thin: it validates inputs, calls the Supabase client,
and maps rows to the spec's response shape. DB errors bubble up as 500;
4xx is reserved for "client gave us something we can't act on."
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from .deps import CurrentUser, get_current_user, get_supabase_admin, get_supabase_user

_logger = logging.getLogger("trackr.auth")

router = APIRouter()


# EmailStr kept loose (str) so `.test` / `.local` TLDs work for the
# seeded test admin. Supabase is the source of truth for delivery.
EmailStr = str


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
    # Convenience for the parent home screen — avoids a second
    # round-trip just to render "Picking up {child_name}".
    linked_child_name: str | None = None
    email: str | None = None
    must_change_password: bool = False


class LoginResponse(BaseModel):
    access_token: str
    profile: Profile


class ResetRequest(BaseModel):
    email: EmailStr


class ResetResponse(BaseModel):
    ok: bool = True


class UpdateMeRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    # Phone: empty string is treated as "clear" by treating "" as None
    # before persisting (see update_me). Length-bounded so a 10MB body
    # can't be smuggled through.
    phone: str | None = Field(default=None, min_length=1, max_length=40)


class CompletePasswordChangeRequest(BaseModel):
    password: str = Field(min_length=6, max_length=128)


class CompletePasswordChangeResponse(BaseModel):
    ok: bool = True

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_data(response: Any) -> Any | None:
    return getattr(response, "data", None)


def _load_profile(user: CurrentUser, supabase) -> Profile:
    """Read the profile row + the joined school name."""
    try:
        res = (
            supabase.table("profiles")
            .select("id, role, school_id, display_name, phone, linked_student_id, must_change_password")
            .eq("id", user.user_id)
            .maybe_single()
            .execute()
        )
        rows = _get_data(res)
    except Exception as exc:
        # RLS or transient error. Log so ops can diagnose, then surface
        # as 404 rather than 500 — the frontend will prompt the user to
        # sign in again.
        _logger.warning("auth/me profile load failed for user %s: %s", user.user_id, exc)
        rows = None
    if rows is None:
        try:
            rows = _get_data(
                supabase.table("profiles")
                .select("id, role, school_id, display_name, phone, linked_student_id")
                .eq("id", user.user_id)
                .maybe_single()
                .execute()
            )
        except Exception as exc:
            _logger.warning("auth/me profile fallback load failed for user %s: %s", user.user_id, exc)
            rows = None
    if rows is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Profile not found.",
        )
    school_name = ""
    try:
        school_row = _get_data(
            supabase.table("schools")
            .select("name")
            .eq("id", rows["school_id"])
            .maybe_single()
            .execute()
        )
        if school_row:
            school_name = school_row.get("name", "")
    except Exception:
        # Profile exists but school row missing — surface empty string
        # rather than 500 the whole endpoint.
        school_name = ""
    # If this is a parent profile, also resolve the linked child's
    # name so the home screen can render "Picking up {child_name}"
    # without a second round-trip. Failure here is non-fatal — the
    # client falls back to "your child".
    linked_child_name: str | None = None
    linked_student_id = rows.get("linked_student_id")
    if linked_student_id:
        try:
            student_row = _get_data(
                supabase.table("students")
                .select("name")
                .eq("id", linked_student_id)
                .maybe_single()
                .execute()
            )
            if student_row:
                linked_child_name = student_row.get("name")
        except Exception:
            linked_child_name = None
    return Profile(
        id=rows["id"],
        role=rows["role"],
        school_id=rows["school_id"],
        school_name=school_name,
        display_name=rows["display_name"],
        phone=rows.get("phone"),
        linked_student_id=linked_student_id,
        linked_child_name=linked_child_name,
        email=user.email,
        must_change_password=rows.get("must_change_password", False),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/login", response_model=LoginResponse)
def login(
    body: LoginRequest,
    # We need TWO Supabase clients here: the user-scoped one to sign
    # in (no Authorization header yet, so it acts like anon), and the
    # service-role one to read the post-login profile row regardless
    # of RLS. Per spec §3, no RLS in MVP — but if RLS is ever added,
    # the profile lookup would silently 404 for every login if we
    # used the user client.
    supabase=Depends(get_supabase_user),
    supabase_admin=Depends(get_supabase_admin),
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
        auth_role=(user_meta.get("role") or "authenticated"),
        email=auth.user.email,
    )
    # Force a profile load on the admin client so the route stays
    # correct when RLS is eventually turned on.
    profile = _load_profile(user, supabase_admin)
    # The login response should expose the APP role, not the auth role.
    user.app_role = profile.role
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
        # Treat "" as "clear the phone field" — the Pydantic model only
        # allows length>=1, so we explicitly map an empty string to None
        # before persisting (the DB column is nullable).
        patch["phone"] = body.phone if body.phone.strip() else None
    if patch:
        (
            supabase.table("profiles")
            .update(patch)
            .eq("id", user.user_id)
            .execute()
        )
    return _load_profile(user, supabase)


@router.post("/complete-password-change", response_model=CompletePasswordChangeResponse)
def complete_password_change(
    body: CompletePasswordChangeRequest,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
) -> CompletePasswordChangeResponse:
    if not user.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Signed-in user email is unavailable.",
        )
    try:
        ok = supabase.rpc(
            "admin_reset_auth_password",
            {"p_email": user.email, "p_new_password": body.password},
        ).execute()
        if not _get_data(ok):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No auth.users row for this account.",
            )
        supabase.table("profiles").update({"must_change_password": False}).eq("id", user.user_id).execute()
        return CompletePasswordChangeResponse()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not complete password change: {exc}",
        ) from exc
