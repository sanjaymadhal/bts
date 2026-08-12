"""Members: invite + list + delete + resend-invite.

Uses a server-generated temp password + direct `auth.users` insert via
the `admin_create_auth_user` RPC (see `scripts/create_auth_user_rpc.sql`),
then sends the welcome email over Gmail SMTP. This deliberately avoids
Supabase's hosted `auth.admin.inviteUserByEmail()` endpoint, which is
rate-limited per project and started rejecting our admin users in
testing with "email rate limit exceeded".

The `handle_new_auth_user` trigger (migrations/004_auth_trigger.sql)
still creates the public.profiles row, so downstream code is
unaffected.

Create flow:
1. Admin enters email + display name + role in the app.
2. Validate email, role, and check for existing invitations.
3. Generate a random temp password.
4. Call `admin_create_auth_user` RPC → inserts auth.users row.
5. Trigger `handle_new_auth_user()` creates public.profiles.
6. Send the welcome email over Gmail SMTP (temp password + role).
7. Trackr records the invitation in public.invitations with
   `email_delivered` reflecting the SMTP result.
8. Return success with invitation details.

Resend flow:
1. Admin clicks resend on an existing invitation.
2. Generate a fresh temp password.
3. Call `admin_reset_auth_password` RPC to rotate the auth row.
4. Re-send the welcome email over Gmail SMTP.
5. Update the invitations record with the new send timestamp.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import smtplib
import string
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from .config import get_settings
from .deps import (
    CurrentUser,
    get_current_user,
    get_supabase_admin,
    load_app_role,
)

router = APIRouter()

EmailStr = str

_logger = logging.getLogger("trackr.invites")

# Plain-text alphabet for the temp password. Deliberately avoids the
# easily-confused characters `O0Il1` so the user can read it back off
# their email without typos.
_TEMP_PASSWORD_ALPHABET = "".join(
    c for c in (string.ascii_letters + string.digits) if c not in "O0Il1"
)
_TEMP_PASSWORD_LENGTH = 12


def _generate_temp_password() -> str:
    """Cryptographically-random one-time password for the new account."""
    return "".join(
        secrets.choice(_TEMP_PASSWORD_ALPHABET)
        for _ in range(_TEMP_PASSWORD_LENGTH)
    )


def _get_data(res):
    """Safely extract `.data` from a supabase-py response which may be None."""
    if res is None:
        return None
    return getattr(res, "data", None)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class InvitationCreate(BaseModel):
    email: EmailStr
    role: str = Field(pattern=r"^(admin|driver|staff|parent|student)$")
    display_name: str = Field(min_length=1, max_length=120)
    linked_student_id: Optional[str] = None
    # Parent-only optional field. Lets the admin wire the parent to a
    # bus at invite time so the home screen has a live bus to render
    # immediately after the parent accepts. Drivers and other roles
    # ignore this.
    assigned_bus_id: Optional[str] = None


class InvitationOut(BaseModel):
    id: str
    email: str
    role: str
    display_name: str
    expires_at: str
    accepted_user_id: Optional[str] = None
    email_delivered: bool = False
    email_last_error: Optional[str] = None
    email_via: Optional[str] = None
    email_sent_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_duplicate_email_error(exc: Exception) -> bool:
    """Check if error indicates email already exists in invitations or auth."""
    text = str(exc).lower()
    return any(marker in text for marker in ["duplicate", "already", "unique constraint"])


def _is_missing_column_error(exc: Exception) -> bool:
    """Detect PostgREST 'column doesn't exist' error for schema migration fallback."""
    msg = str(exc).lower()
    return ("could not find" in msg or "does not exist" in msg) and ("column" in msg or "schema cache" in msg)


async def _resolve_school(supabase_admin) -> dict:
    """Resolve the configured school's {id, name}. Falls back to DPS East."""
    settings = get_settings()
    try:
        res = (
            supabase_admin.table("schools")
            .select("id, name")
            .eq("code", settings.SCHOOL_CODE)
            .maybe_single()
            .execute()
        )
        row = _get_data(res)
        if row:
            return row
    except Exception:
        pass
    return {"id": "00000000-0000-0000-0000-000000000001", "name": settings.SCHOOL_CODE}


def _hash_pw(pw: str) -> str:
    """Hash the one-time temp password for durable invitation storage."""
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()


def _send_invite_email(
    *,
    to_email: str,
    display_name: str,
    role: str,
    temp_password: str,
    school_name: str,
) -> tuple[bool, Optional[str], Optional[str]]:
    """Send the welcome / re-invite email via Gmail SMTP.

    Returns ``(delivered, last_error, via)``:
      - ``delivered`` — True iff the SMTP ``sendmail`` call returned
        without raising.
      - ``last_error`` — short, user-friendly string on failure
        (suitable for surfacing in the admin UI).
      - ``via`` — identifier for the SMTP envelope sender so the admin
        UI can render a "via …" badge.

    Never raises: SMTP errors are caught and returned as
    ``(False, message)`` so the create / resend route can still record
    the invitation row and surface the failure state.
    """
    settings = get_settings()
    if not settings.SMTP_USER or not settings.SMTP_APP_PASSWORD:
        # Operator hasn't configured SMTP — fail loudly so the issue is
        # visible in the admin UI rather than silently no-oping.
        return (
            False,
            "SMTP not configured (set SMTP_USER + SMTP_APP_PASSWORD in .env)",
            None,
        )

    from_addr = settings.EMAIL_FROM
    subject = f"Your {school_name} Trackr account"
    body = (
        f"Hi {display_name},\n\n"
        f"You've been added to {school_name} Trackr as a {role}.\n\n"
        f"Sign in with:\n"
        f"  Email:    {to_email}\n"
        f"  Password: {temp_password}\n\n"
        f"You'll be asked to set a new password on first sign-in.\n\n"
        f"— {school_name} Trackr\n"
    )

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as smtp:
            if settings.SMTP_USE_TLS:
                smtp.starttls()
            smtp.login(settings.SMTP_USER, settings.SMTP_APP_PASSWORD)
            smtp.send_message(msg)
        return (True, None, from_addr)
    except (smtplib.SMTPAuthenticationError, smtplib.SMTPException, OSError) as exc:
        _logger.warning(
            "[invite] SMTP send failed for %s via %s: %s",
            to_email,
            settings.SMTP_HOST,
            exc,
        )
        # Trim verbose exception text — the admin UI shows this in a
        # small line under the email-delivered badge.
        short = str(exc).splitlines()[0][:160] if str(exc) else exc.__class__.__name__
        return (False, short, None)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=InvitationOut, status_code=status.HTTP_201_CREATED)
async def create_invitation(
    body: InvitationCreate,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase_admin=Depends(get_supabase_admin),
) -> InvitationOut:
    """Create an invitation and invite the user via Supabase Auth.
    
    Admin-only. Uses Supabase's official inviteUserByEmail() API.
    The database trigger automatically creates the Trackr profile.
    """
    # Admin-only gate
    if user.app_role is None:
        user.app_role = load_app_role(user, supabase_admin)
    if user.app_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can create invitations.",
        )

    # 1) Validate role
    valid_roles = {"admin", "driver", "staff", "parent", "student"}
    if body.role not in valid_roles:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid role. Must be one of: {', '.join(valid_roles)}",
        )

    # 2) Check for duplicate invitation beforehand
    try:
        res = (
            supabase_admin.table("invitations")
            .select("id")
            .eq("email", body.email)
            .maybe_single()
            .execute()
        )
        if _get_data(res):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An invitation for this email already exists.",
            )
    except HTTPException:
        raise
    except Exception as exc:
        _logger.warning("[invite] duplicate-check query failed: %s", exc)
        # Non-fatal; proceed

    # 3) Resolve school
    school = await _resolve_school(supabase_admin)

    # 4) Generate a fresh temp password and prepare user metadata
    temp_password = _generate_temp_password()
    user_metadata = {
        "role": body.role,
        "display_name": body.display_name,
        "must_change_password": True,
    }
    if body.linked_student_id:
        user_metadata["linked_student_id"] = body.linked_student_id
    if body.assigned_bus_id:
        user_metadata["assigned_bus_id"] = body.assigned_bus_id

    # 5) Create the auth.users row directly via the security-definer RPC
    # defined in scripts/create_auth_user_rpc.sql. This bypasses
    # Supabase's hosted invite endpoint (which is rate-limited) and
    # the `on_auth_user_created` trigger, both of which were causing
    # the "RPC error" / "email rate limit exceeded" the admin was
    # seeing in testing.
    auth_user_id: Optional[str] = None
    auth_error: Optional[str] = None
    duplicate_email = False

    try:
        _logger.info(
            "[invite] admin_create_auth_user for %s (role=%s)",
            body.email,
            body.role,
        )
        rpc_res = supabase_admin.rpc(
            "admin_create_auth_user",
            {
                "p_email": body.email,
                "p_password": temp_password,
                "p_user_metadata": user_metadata,
            },
        ).execute()
        rpc_data = _get_data(rpc_res)
        # The RPC returns the new uuid; supabase-py may surface it as
        # a list, a string, or a dict depending on version. Coerce.
        if isinstance(rpc_data, dict):
            auth_user_id = rpc_data.get("admin_create_auth_user") or str(rpc_data)
        elif isinstance(rpc_data, list) and rpc_data:
            auth_user_id = str(rpc_data[0])
        elif isinstance(rpc_data, str):
            auth_user_id = rpc_data
        else:
            auth_user_id = str(rpc_data) if rpc_data is not None else None

        if auth_user_id and auth_user_id.startswith("00000000-0000-0000-0000"):
            # The RPC failed silently and returned the sentinel null
            # uuid. This is what the RPC does when the auth.users row
            # already exists — fall through to the duplicate-email
            # recovery below.
            duplicate_email = True
            auth_user_id = None
            auth_error = None
            _logger.info(
                "[invite] admin_create_auth_user returned sentinel uuid "
                "for %s; trying duplicate-email recovery",
                body.email,
            )

        _logger.info(
            "[invite] auth row created for %s; user_id=%s",
            body.email,
            auth_user_id,
        )
    except Exception as exc:
        exc_text = str(exc).lower()
        if _is_duplicate_email_error(exc):
            duplicate_email = True
            _logger.warning(
                "[invite] auth.users already has %s; reusing existing id",
                body.email,
            )
        else:
            auth_error = str(exc)
            _logger.error(
                "[invite] admin_create_auth_user failed for %s: %s",
                body.email,
                exc,
            )

    # If the RPC either raised a duplicate-email error OR returned the
    # sentinel null uuid, try to look up the existing auth.users row
    # and reuse it so the admin sees a working invitation + email
    # instead of a 502.
    if duplicate_email and not auth_user_id:
        try:
            list_res = supabase_admin.auth.admin.list_users()
            for au in (getattr(list_res, "users", None) or []):
                au_email = getattr(au, "email", None)
                if au_email and au_email.lower() == body.email.lower():
                    auth_user_id = au.id
                    break
        except Exception as lookup_exc:
            _logger.warning(
                "[invite] list_users for duplicate-email recovery failed: %s",
                lookup_exc,
            )
        if auth_user_id:
            # Rotate the password on the existing row so the welcome
            # email we send below uses a fresh temp pw.
            try:
                supabase_admin.rpc(
                    "admin_reset_auth_password",
                    {
                        "p_email": body.email,
                        "p_new_password": temp_password,
                    },
                ).execute()
            except Exception as rotate_exc:
                _logger.warning(
                    "[invite] admin_reset_auth_password during "
                    "duplicate-email recovery failed: %s",
                    rotate_exc,
                )
                auth_error = (
                    "auth.users has this email but the password "
                    "could not be rotated"
                )
                auth_user_id = None

    if not auth_user_id:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Failed to create the auth account: {auth_error}. "
                "Ensure scripts/create_auth_user_rpc.sql has been run "
                "in the Supabase SQL Editor."
            ),
        )

    # 5b) Defense in depth: ensure a public.profiles row exists for
    # the auth user. The `handle_new_auth_user` trigger (migration
    # 004) is the canonical source — if it's installed and working,
    # the row is already there. If it isn't (project never ran
    # migration 004), insert the profile directly so downstream code
    # (bus sheet driver picker, login, etc.) still works.
    try:
        existing = (
            supabase_admin.table("profiles")
            .select("id")
            .eq("id", auth_user_id)
            .maybe_single()
            .execute()
        )
        if _get_data(existing) is None:
            _logger.info(
                "[invite] no profile row for %s; inserting one (trigger missing?)",
                auth_user_id,
            )
            profile_payload = {
                "id": auth_user_id,
                "role": body.role,
                "school_id": school["id"],
                "display_name": body.display_name,
                "linked_student_id": body.linked_student_id,
                "must_change_password": True,
            }
            try:
                supabase_admin.table("profiles").insert(profile_payload).execute()
            except Exception as profile_exc:
                # If insert fails due to unique violation (trigger
                # raced us), the row exists and we're good.
                if not _is_duplicate_email_error(profile_exc):
                    _logger.warning(
                        "[invite] profile insert for %s failed: %s",
                        auth_user_id,
                        profile_exc,
                    )
    except Exception as exc:
        _logger.warning(
            "[invite] profile existence check failed for %s: %s",
            auth_user_id,
            exc,
        )

    # 6) Parent-only: if the admin picked a bus at invite time, assign
    # the linked student to it so the parent sees a live bus the
    # moment they accept. Errors are logged and not raised — the
    # parent can be re-assigned later via the admin's move-student
    # sheet, and the auth account + profile are already live.
    if (
        body.role == "parent"
        and body.linked_student_id
        and body.assigned_bus_id
    ):
        try:
            supabase_admin.table("students").update(
                {"bus_id": body.assigned_bus_id}
            ).eq("id", body.linked_student_id).execute()
            _logger.info(
                "[invite] parent=%s linked student=%s now on bus=%s",
                auth_user_id,
                body.linked_student_id,
                body.assigned_bus_id,
            )
        except Exception as assign_exc:
            _logger.warning(
                "[invite] failed to assign student %s to bus %s: %s",
                body.linked_student_id,
                body.assigned_bus_id,
                assign_exc,
            )

    # 7) Record the invitation in Trackr's invitations table.
    # The auth row and profile are now created; if the DB insert fails
    # we log loudly but DO NOT roll back the auth row — leaving a
    # recoverable auth.users row is better than a 500 that the admin
    # has to manually triage. The duplicate check at the top of the
    # handler covers the common "retry the same email" case.
    insert_payload = {
        "email": body.email,
        "role": body.role,
        "display_name": body.display_name,
        "accepted_user_id": auth_user_id,
        "temp_password_hash": _hash_pw(temp_password),
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=14)).isoformat(),
        # Delivery columns from migration 002 — included so the row is
        # fully populated in one INSERT instead of two round-trips.
        "email_delivered": False,
        "email_last_error": None,
        "email_via": None,
        "email_sent_at": None,
    }

    try:
        res = supabase_admin.table("invitations").insert(insert_payload).execute()
        inserted = _get_data(res)
        _logger.info("[invite] Invitation record created for %s", body.email)
    except Exception as exc:
        _logger.error("[invite] Failed to create invitation record: %s", exc)
        # If the failure is "missing column" (migration 002 hasn't run
        # on this DB), retry with the v1 payload so the row at least
        # gets recorded.
        if _is_missing_column_error(exc):
            v1_payload = {
                "email": body.email,
                "role": body.role,
                "display_name": body.display_name,
                "accepted_user_id": auth_user_id,
                "temp_password_hash": _hash_pw(temp_password),
                "expires_at": (datetime.now(timezone.utc) + timedelta(days=14)).isoformat(),
            }
            try:
                res = supabase_admin.table("invitations").insert(v1_payload).execute()
                inserted = _get_data(res)
                _logger.warning(
                    "[invite] Invitation row recorded without delivery columns — "
                    "run migrations/002_invitation_delivery.sql"
                )
            except Exception as exc2:
                _logger.error(
                    "[invite] v1 payload also failed: %s",
                    exc2,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=(
                        f"Auth account {auth_user_id} was created but the "
                        "invitation row could not be recorded. Please "
                        "remove this email from Supabase auth and retry."
                    ),
                ) from exc2
        else:
            # Non-schema failure (RLS, network, etc.). The auth row
            # and profile are real and live; surface a clear 500 so the
            # admin knows to delete the auth row manually.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"Auth account {auth_user_id} was created but the "
                    "invitation row could not be recorded. Please "
                    "remove this email from Supabase auth and retry. "
                    f"({exc})"
                ),
            ) from exc

    row = inserted[0] if isinstance(inserted, list) else inserted

    # 7) Send the welcome email over Gmail SMTP. This is wrapped in a
    # broad try/except so any SMTP-class exception — including
    # ssl.SSLError on the TLS handshake — is recorded as a delivery
    # failure rather than a 500 that hides the created account.
    delivered, last_error, via = (False, "email not yet attempted", None)
    try:
        delivered, last_error, via = _send_invite_email(
            to_email=body.email,
            display_name=body.display_name,
            role=body.role,
            temp_password=temp_password,
            school_name=school.get("name") or get_settings().SCHOOL_CODE,
        )
    except Exception as exc:
        _logger.error(
            "[invite] SMTP raised unexpectedly for %s: %s",
            body.email,
            exc,
        )
        last_error = f"SMTP raised {exc.__class__.__name__}: {str(exc)[:120]}"

    sent_at = datetime.now(timezone.utc).isoformat() if delivered else None

    update_payload = {
        "email_delivered": delivered,
        "email_last_error": last_error,
        "email_via": via or get_settings().EMAIL_FROM,
        "email_sent_at": sent_at,
    }
    try:
        supabase_admin.table("invitations").update(update_payload).eq("id", row["id"]).execute()
    except Exception as exc:
        # Non-fatal: if the v1-schema fallback was used above, these
        # columns don't exist; otherwise this is a transient PostgREST
        # hiccup and the admin UI will refresh on next pull anyway.
        if not _is_missing_column_error(exc):
            _logger.error("[invite] Failed to update invitation delivery state: %s", exc)

    return InvitationOut(
        id=row["id"],
        email=row["email"],
        role=row["role"],
        display_name=row["display_name"],
        expires_at=row["expires_at"],
        accepted_user_id=row.get("accepted_user_id"),
        email_delivered=delivered,
        email_last_error=last_error,
        email_via=via or get_settings().EMAIL_FROM,
        email_sent_at=sent_at,
    )




@router.get("", response_model=list[InvitationOut])
def list_invitations(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
) -> list[InvitationOut]:
    """List all invitations for the current school. Admin-only."""
    if user.app_role is None:
        user.app_role = load_app_role(user, supabase)
    if user.app_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can list invitations.",
        )

    # Try new schema first, fall back to old
    try:
        res = (
            supabase.table("invitations")
            .select(
                "id, email, role, display_name, expires_at, accepted_user_id, "
                "email_delivered, email_last_error, email_via, email_sent_at"
            )
            .execute()
        )
        rows = _get_data(res) or []
    except Exception as exc:
        if not _is_missing_column_error(exc):
            raise
        # Fall back to old schema
        res = (
            supabase.table("invitations")
            .select("id, email, role, display_name, expires_at, accepted_user_id")
            .execute()
        )
        rows = _get_data(res) or []

    return [
        InvitationOut(
            id=r["id"],
            email=r["email"],
            role=r["role"],
            display_name=r["display_name"],
            expires_at=str(r["expires_at"]),
            accepted_user_id=r.get("accepted_user_id"),
            email_delivered=r.get("email_delivered", True),
            email_last_error=r.get("email_last_error"),
            email_via=r.get("email_via"),
            email_sent_at=(
                str(r["email_sent_at"]) if r.get("email_sent_at") else None
            ),
        )
        for r in rows
    ]


@router.post("/{invitation_id}/resend", response_model=InvitationOut)
async def resend_invitation(
    invitation_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
) -> InvitationOut:
    """Re-send the invitation email. Admin-only.
    
    Calls Supabase's invitation API again to send a fresh invite link.
    """
    if user.app_role is None:
        user.app_role = load_app_role(user, supabase)
    if user.app_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can resend invitations.",
        )

    # 1) Fetch the invitation record
    res = (
        supabase.table("invitations")
        .select("id, email, role, display_name, expires_at, accepted_user_id")
        .eq("id", invitation_id)
        .maybe_single()
        .execute()
    )
    row = _get_data(res)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invitation not found.",
        )

    # 2) Rotate the temp password via the security-definer RPC and
    # re-send the welcome email over Gmail SMTP. The previous version
    # of this handler called `supabase.auth.admin.invite_user_by_email`
    # and chained `.execute()` on the result, which raised
    # `AttributeError: 'UserResponse' object has no attribute 'execute'`
    # and surfaced as the "RPC error" the admin saw when they tapped
    # Resend. Even after that bug, the hosted invite endpoint is
    # rate-limited, so we now use our own SMTP path here too.
    temp_password = _generate_temp_password()
    try:
        _logger.info(
            "[resend] admin_reset_auth_password for %s (invitation_id=%s)",
            row["email"],
            invitation_id,
        )
        rpc_res = supabase.rpc(
            "admin_reset_auth_password",
            {"p_email": row["email"], "p_new_password": temp_password},
        ).execute()
        reset_ok = bool(_get_data(rpc_res))
        if not reset_ok:
            raise RuntimeError(
                "admin_reset_auth_password returned no rows for "
                f"{row['email']}"
            )
        _logger.info(
            "[resend] password rotated for %s; re-sending via SMTP",
            row["email"],
        )
    except Exception as exc:
        _logger.error(
            "[resend] admin_reset_auth_password failed for %s: %s",
            row["email"],
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Could not rotate the account password: {exc}. "
                "Ensure scripts/create_auth_user_rpc.sql has been run."
            ),
        ) from exc

    # 3) Send the new welcome email. SMTP failure here is reflected in
    # the row but doesn't break the response — the auth password WAS
    # rotated, so the admin can ask the user to use the new password
    # or tap Resend again.
    settings = get_settings()
    school_name = settings.SCHOOL_CODE
    try:
        school_row = (
            supabase.table("schools")
            .select("name")
            .eq("code", settings.SCHOOL_CODE)
            .maybe_single()
            .execute()
        )
        if _get_data(school_row):
            school_name = _get_data(school_row).get("name") or school_name
    except Exception:
        pass

    delivered, last_error, via = _send_invite_email(
        to_email=row["email"],
        display_name=row["display_name"],
        role=row["role"],
        temp_password=temp_password,
        school_name=school_name,
    )

    # 4) Update the invitation record with the new send state
    sent_at = datetime.now(timezone.utc).isoformat()
    update_payload = {
        "email_sent_at": sent_at if delivered else None,
        "email_delivered": delivered,
        "email_last_error": last_error,
        "email_via": via or settings.EMAIL_FROM,
    }
    try:
        supabase.table("invitations").update(update_payload).eq("id", invitation_id).execute()
    except Exception as exc:
        if not _is_missing_column_error(exc):
            _logger.error("[resend] Failed to update invitation record: %s", exc)
        # Non-fatal; return success anyway

    return InvitationOut(
        id=row["id"],
        email=row["email"],
        role=row["role"],
        display_name=row["display_name"],
        expires_at=str(row["expires_at"]),
        accepted_user_id=row.get("accepted_user_id"),
        email_delivered=delivered,
        email_last_error=last_error,
        email_via=via or settings.EMAIL_FROM,
        email_sent_at=sent_at if delivered else None,
    )


@router.delete("/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_invitation(
    invitation_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
):
    """Delete an invitation. Admin-only.

    Also tears down the linked ``profiles`` row and the underlying
    ``auth.users`` record (when present) so the user actually stops
    appearing in the school's profile directory. Without this, the
    bus sheet's driver picker kept listing drivers whose
    invitation had been removed — the admin would see the row
    vanish from "Accounts created" but stay in the dropdown, and
    re-invite the same email.
    """
    if user.app_role is None:
        user.app_role = load_app_role(user, supabase)
    if user.app_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can delete invitations.",
        )

    # 1) Look up the invitation first so we know which profile/auth row
    #    to clean up. We need this BEFORE deleting the invitation row
    #    because the invitation carries `accepted_user_id` — the
    #    profile id.
    try:
        inv_res = (
            supabase.table("invitations")
            .select("id, accepted_user_id, email")
            .eq("id", invitation_id)
            .maybe_single()
            .execute()
        )
        inv_row = _get_data(inv_res)
    except Exception as exc:
        _logger.warning("[invite-delete] lookup of %s failed: %s", invitation_id, exc)
        inv_row = None

    if not inv_row:
        # Nothing to clean up downstream; fall through to the
        # canonical delete which will 204 (no rows match).
        supabase.table("invitations").delete().eq("id", invitation_id).execute()
        return

    profile_id = inv_row.get("accepted_user_id")
    email = inv_row.get("email")

    # 2) Best-effort: clear `driver_id` on any bus that referenced
    #    this profile so the bus doesn't keep a dangling FK. If the
    #    profile row has a foreign key to buses (driver_id), a plain
    #    delete below would fail with a constraint violation; the
    #    PATCH-then-DELETE is idempotent and safe.
    if profile_id:
        try:
            supabase.table("buses").update({"driver_id": None}).eq(
                "driver_id", profile_id
            ).execute()
        except Exception as exc:
            _logger.warning(
                "[invite-delete] could not clear driver_id on buses for %s: %s",
                profile_id,
                exc,
            )

    # 3) Delete the profile row. Done before auth.users so that if
    #    auth delete fails, the profile is at least gone and the bus
    #    sheet's driver picker (which queries /profiles?role=driver)
    #    stops listing them. The cached driver map on the mobile
    #    side is invalidated by the UI on the matching `Remove`
    #    tap.
    if profile_id:
        try:
            supabase.table("profiles").delete().eq("id", profile_id).execute()
        except Exception as exc:
            _logger.warning(
                "[invite-delete] profile delete for %s failed: %s",
                profile_id,
                exc,
            )

    # 4) Best-effort: drop the auth.users row so the email truly
    #    can't sign in. If the operator hasn't granted the service
    #    role delete permission this raises — we log and continue,
    #    because the invitation + profile are already gone and the
    #    admin should at least see the row leave the list.
    if email:
        try:
            # Look up the auth user by email (admin.list_users is the
            # supported read path) and remove them. We can't filter
            # by email on the delete call directly.
            listed = supabase.auth.admin.list_users()
            target_id = None
            for au in getattr(listed, "users", None) or []:
                au_email = getattr(au, "email", None)
                if au_email and au_email.lower() == email.lower():
                    target_id = au.id
                    break
            if target_id:
                supabase.auth.admin.delete_user(target_id)
        except Exception as exc:
            _logger.warning(
                "[invite-delete] auth.users delete for %s failed: %s",
                email,
                exc,
            )

    # 5) Finally delete the invitation row itself.
    supabase.table("invitations").delete().eq("id", invitation_id).execute()