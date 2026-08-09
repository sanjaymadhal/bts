"""Invitations: create + list + delete.

Create flow:
1. Generate a random temp password.
2. Use the service-role client to `auth.admin.create_user` (email_confirm
   bypasses the verification step — see backend spec §3).
3. Insert a row in `invitations` capturing email/role/display_name + the
   hash of the temp password (we never store the plaintext).
4. Send the invite email via Resend. Failures here don't undo the
   invitation row — the admin can re-send manually.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from .config import get_settings
from ..deps import CurrentUser, get_current_user, get_supabase_admin, get_supabase_user

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class InvitationCreate(BaseModel):
    email: EmailStr
    role: str = Field(pattern=r"^(admin|driver|staff|parent|student)$")
    display_name: str = Field(min_length=1, max_length=120)
    linked_student_id: str | None = None


class InvitationOut(BaseModel):
    id: str
    email: str
    role: str
    display_name: str
    expires_at: str
    accepted_user_id: str | None = None


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def _send_invite_email(to: str, display_name: str, temp_pw: str, school_name: str) -> bool:
    """Send the invite via Resend. Returns True on success.

    Falls back to stdout in dev so tests run without a Resend key.
    """
    import logging

    settings = get_settings()
    if not settings.RESEND_API_KEY:
        # Dev fallback: log so the developer can read the temp password.
        logging.warning(
            "[invite] %s (%s) temp_pw=%s school=%s",
            to, display_name, temp_pw, school_name,
        )
        return True

    try:
        import resend

        resend.api_key = settings.RESEND_API_KEY
        resend.Emails.send({
            "from": settings.EMAIL_FROM,
            "to": [to],
            "subject": f"Welcome to {school_name}",
            "html": (
                f"<p>Hi {display_name},</p>"
                f"<p>You've been invited to {school_name}'s Trackr app.</p>"
                f"<p>Sign in with this email and your temporary password: "
                f"<code>{temp_pw}</code></p>"
                f"<p>You'll be asked to set a new password on first sign in.</p>"
            ),
        })
        return True
    except Exception:
        return False


def _hash_pw(pw: str) -> str:
    """Lightweight hash for storage. Not for production auth — we never
    log in via this hash; the temp password is delivered in email and
    discarded. bcrypt would be overkill for a value with a 24h TTL.
    """
    import hashlib

    return hashlib.sha256(pw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=InvitationOut, status_code=status.HTTP_201_CREATED)
def create_invitation(
    body: InvitationCreate,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase_user=Depends(get_supabase_user),
    supabase_admin=Depends(get_supabase_admin),
) -> InvitationOut:
    settings = get_settings()
    temp_pw = secrets.token_urlsafe(12)
    try:
        created = supabase_admin.auth.admin.create_user({
            "email": body.email,
            "password": temp_pw,
            "email_confirm": True,
            "user_metadata": {"role": body.role, "display_name": body.display_name},
        })
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not create user (email may already be in use).",
        ) from exc

    auth_user_id = created.user.id if created and created.user else None

    # Insert profile row so /auth/login works immediately.
    school_row = (
        supabase_user.table("schools")
        .select("id, name")
        .eq("code", settings.SCHOOL_CODE)
        .single()
        .execute()
        .data
        or {"id": "00000000-0000-0000-0000-000000000001", "name": settings.SCHOOL_CODE}
    )

    try:
        supabase_user.table("profiles").insert({
            "id": auth_user_id,
            "role": body.role,
            "school_id": school_row["id"],
            "display_name": body.display_name,
            "linked_student_id": body.linked_student_id,
        }).execute()
    except Exception:
        # Profile may already exist if the same email was re-invited.
        pass

    expires_at = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
    inserted = (
        supabase_user.table("invitations")
        .insert({
            "email": body.email,
            "role": body.role,
            "display_name": body.display_name,
            "accepted_user_id": auth_user_id,
            "temp_password_hash": _hash_pw(temp_pw),
            "expires_at": expires_at,
        })
        .execute()
        .data
    )
    row = inserted[0]

    _send_invite_email(body.email, body.display_name, temp_pw, school_row["name"])

    return InvitationOut(
        id=row["id"],
        email=row["email"],
        role=row["role"],
        display_name=row["display_name"],
        expires_at=row["expires_at"],
        accepted_user_id=row.get("accepted_user_id"),
    )


@router.get("", response_model=list[InvitationOut])
def list_invitations(
    _user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_user),
) -> list[InvitationOut]:
    rows = (
        supabase.table("invitations")
        .select("id, email, role, display_name, expires_at, accepted_user_id")
        .execute()
        .data
        or []
    )
    return [InvitationOut(**r) for r in rows]


@router.delete("/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_invitation(
    invitation_id: str,
    _user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_user),
) -> None:
    supabase.table("invitations").delete().eq("id", invitation_id).execute()
