"""Notification fan-out — server-side trigger evaluation.

The simulator calls `emit_stop_transition` after each tick that crosses
a stop boundary. That helper:

1. Writes one row per admin in the school (`kind='bus_at_stop'`).
2. For every parent linked to a student on the bus, computes how many
   stops away the bus is from the parent's stop and writes a row
   accordingly (`bus_two_stops_away` for K=2, `bus_arriving` for K=0/1).
3. Optionally dispatches an Expo push per row.

The engine is deliberately defensive: every Supabase call is wrapped in
try/except so a single malformed row can't crash the simulator's loop.
Failures are logged at WARNING level so ops can spot bad data.

Public API:
  - `emit_stop_transition(supabase_admin, bus_id, bus_number, stop_index, stop_name)`
  - `send_expo_push(token, title, body, data=None)` (exported so tests
    can monkey-patch it cleanly)

Triggered by: `app/simulator.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

import httpx

from .config import get_settings

_logger = logging.getLogger("trackr.notifications")

#: HTTP endpoint for Expo's push service. Documented at
#: https://docs.expo.dev/push-notifications/sending-notifications/
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"

#: Default batch size — Expo accepts up to 100 messages per call.
EXPO_PUSH_BATCH = 100

#: How many stops between the bus's current position and a parent's
#: pickup stop should trigger the "two stops away" notification.
TWO_STOPS_AWAY_THRESHOLD = 2

#: Minimum useful push reliability — Expo returns 200 for accepted
#: messages and 429 for "rate exceeded". We treat both as success and
#: only treat non-2xx as a hard failure.
_HTTP_OK_RANGE = (200, 300)


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------


def _get_data(res: Any) -> Any:
    """Safely extract `.data` from a supabase-py response."""
    if res is None:
        return None
    return getattr(res, "data", None)


async def _run_supabase(supabase_admin, fn, *args, **kwargs) -> Any:
    """Run a sync Supabase call on the default executor.

    supabase-py is sync; the simulator's tick loop is async, so any
    call here blocks the event loop for the round-trip. `to_thread`
    keeps the loop responsive while the fan-out does its work.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


# ---------------------------------------------------------------------------
# Expo push transport
# ---------------------------------------------------------------------------


async def send_expo_push(
    token: str,
    title: str,
    body: str,
    data: Optional[dict[str, Any]] = None,
) -> bool:
    """Send a single push message via Expo's HTTP API.

    Returns True on accepted delivery, False on transport error.
    Non-2xx HTTP responses are treated as "not delivered"; the caller
    (the engine) decides whether to retry. We do NOT raise — push
    failure should never block the notification row from being
    written in the database.
    """
    if not token:
        return False
    settings = get_settings()
    if not settings.EXPO_PUSH_ENABLED:
        # Operator has explicitly disabled push delivery. We still
        # return True so the rest of the fan-out path "succeeds".
        _logger.debug("expo push disabled; skipping send to %s", token[:24])
        return True

    payload = [
        {
            "to": token,
            "title": title,
            "body": body,
            "sound": "default",
            "data": data or {},
        }
    ]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                EXPO_PUSH_URL,
                content=json.dumps(payload),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
    except Exception as exc:
        _logger.warning("expo push transport failure for %s: %s", token[:24], exc)
        return False

    if r.status_code not in _HTTP_OK_RANGE:
        _logger.warning(
            "expo push rejected for %s: %s — %s",
            token[:24],
            r.status_code,
            r.text[:200],
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Stop matching
# ---------------------------------------------------------------------------


def _match_parent_stop_index(
    student_stop: Optional[str],
    schedule: list[dict[str, Any]],
) -> Optional[int]:
    """Find the schedule index whose `.name` matches the student's stop.

    Falls back to None when the student has no `stop` text or no
    schedule item matches. We case-fold + strip so a stray space in
    the admin UI doesn't break the match.
    """
    if not student_stop or not schedule:
        return None
    target = student_stop.strip().lower()
    for i, stop in enumerate(schedule):
        name = (stop.get("name") or "").strip().lower()
        if name == target:
            return i
    return None


def _stops_away(parent_idx: int, bus_idx: int, total: int) -> int:
    """Forward distance from `bus_idx` to `parent_idx` on the loop.

    Wraps around the schedule end (it's circular). Returns 0 when
    the bus is currently at the parent's stop.
    """
    if total <= 0:
        return 0
    return (parent_idx - bus_idx) % total


# ---------------------------------------------------------------------------
# Recipient resolution
# ---------------------------------------------------------------------------


async def _list_admin_user_ids(supabase_admin) -> list[str]:
    """Return all admin user_ids for the single-tenant school.

    Failure falls back to [] so the fan-out skips the admin row
    instead of crashing.
    """
    try:
        res = supabase_admin.table("profiles").select("id, role").eq("role", "admin").execute()
        rows = _get_data(res) or []
        return [r["id"] for r in rows if r.get("id")]
    except Exception as exc:
        _logger.warning("list admins failed: %s", exc)
        return []


async def _list_students_with_parents(supabase_admin, bus_id: str) -> list[dict[str, Any]]:
    """Return every student assigned to `bus_id` with their stop name.

    Each row carries the parent id (if the parent profile has been
    linked). The fan-out ignores students with no parent — they're
    not visible to anyone in the app's notification surface.
    """
    try:
        student_res = (
            supabase_admin.table("students")
            .select("id, name, stop")
            .eq("bus_id", bus_id)
            .execute()
        )
        students = _get_data(student_res) or []

        if not students:
            return []

        student_ids = [s.get("id") for s in students if s.get("id")]
        parent_map: dict[str, str] = {}
        if student_ids:
            parent_res = (
                supabase_admin.table("profiles")
                .select("id, linked_student_id, role")
                .eq("role", "parent")
                .in_("linked_student_id", student_ids)
                .execute()
            )
            parent_rows = _get_data(parent_res) or []
            for p in parent_rows:
                linked_student_id = p.get("linked_student_id")
                parent_id = p.get("id")
                if linked_student_id and parent_id:
                    parent_map[linked_student_id] = parent_id
    except Exception as exc:
        _logger.warning("list students on bus %s failed: %s", bus_id, exc)
        return []

    out: list[dict[str, Any]] = []
    for s in students:
        out.append(
            {
                "student_id": s.get("id"),
                "student_name": s.get("name"),
                "stop": s.get("stop"),
                "parent_id": parent_map.get(s.get("id")),
            }
        )
    return out


async def _fetch_bus(supabase_admin, bus_id: str) -> Optional[dict[str, Any]]:
    try:
        res = (
            supabase_admin.table("buses")
            .select("id, number, route, schedule")
            .eq("id", bus_id)
            .maybe_single()
            .execute()
        )
        return _get_data(res)
    except Exception as exc:
        _logger.warning("fetch bus %s failed: %s", bus_id, exc)
        return None


async def _get_push_token(supabase_admin, user_id: str) -> Optional[str]:
    try:
        res = (
            supabase_admin.table("push_tokens")
            .select("token")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        row = _get_data(res)
        return row.get("token") if row else None
    except Exception as exc:
        _logger.warning("fetch push token for %s failed: %s", user_id, exc)
        return None


# ---------------------------------------------------------------------------
# Row insertion
# ---------------------------------------------------------------------------


async def _insert_notification(
    supabase_admin,
    *,
    user_id: str,
    kind: str,
    title: str,
    body: str,
    bus_id: Optional[str],
    student_id: Optional[str],
    data: dict[str, Any],
) -> None:
    """Insert one notification row. Errors are logged, not raised."""
    payload = {
        "user_id": user_id,
        "kind": kind,
        "title": title,
        "body": body,
        "bus_id": bus_id,
        "student_id": student_id,
        "data": data,
    }
    try:
        await _run_supabase(supabase_admin, supabase_admin.table("notifications").insert(payload).execute)
    except Exception as exc:
        _logger.warning(
            "notification insert failed (user=%s kind=%s): %s",
            user_id,
            kind,
            exc,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def emit_stop_transition(
    supabase_admin,
    *,
    bus_id: str,
    bus_number: str,
    stop_index: int,
    stop_name: str,
) -> int:
    """Fan out notifications for one bus → stop transition.

    Called by the simulator after it has just upserted
    `bus_positions` and confirmed the bus moved to a new stop.

    Returns the number of notification rows written (best-effort;
    the count is for logging and tests, not for the API).
    """
    bus = await _fetch_bus(supabase_admin, bus_id)
    if bus is None:
        _logger.warning("emit_stop_transition: bus %s not found", bus_id)
        return 0
    schedule = bus.get("schedule") or []
    if not schedule or stop_index < 0 or stop_index >= len(schedule):
        _logger.warning(
            "emit_stop_transition: bad stop_index=%s for bus %s (len=%s)",
            stop_index, bus_id, len(schedule),
        )
        return 0

    data = {
        "bus_id": bus_id,
        "bus_number": bus_number,
        "stop_index": stop_index,
        "stop_name": stop_name,
    }
    written = 0

    # 1) Admin rows — one per admin user in the school.
    admin_ids = await _list_admin_user_ids(supabase_admin)
    for admin_id in admin_ids:
        await _insert_notification(
            supabase_admin,
            user_id=admin_id,
            kind="bus_at_stop",
            title=f"Bus {bus_number} arrived",
            body=f"Bus {bus_number} just arrived at {stop_name}.",
            bus_id=bus_id,
            student_id=None,
            data=data,
        )
        written += 1
        # Push is optional for admins. Skip the OS push unless the
        # admin has enrolled a token — saves the round-trip.
        token = await _get_push_token(supabase_admin, admin_id)
        if token:
            await send_expo_push(
                token,
                f"Bus {bus_number} arrived",
                f"Bus {bus_number} just arrived at {stop_name}.",
                {"busId": bus_id},
            )

    # 2) Parent rows — distance from current stop to parent's stop.
    students = await _list_students_with_parents(supabase_admin, bus_id)
    for student in students:
        parent_id = student.get("parent_id")
        if not parent_id:
            continue
        parent_idx = _match_parent_stop_index(student.get("stop"), schedule)
        if parent_idx is None:
            continue
        away = _stops_away(parent_idx, stop_index, len(schedule))
        if away == 0:
            kind, title, body = (
                "bus_arriving",
                "Bus arriving",
                f"Bus {bus_number} is at your stop — {stop_name}.",
            )
        elif away <= TWO_STOPS_AWAY_THRESHOLD:
            eta_min = away * 2  # rough heuristic: ~2 min per stop
            kind, title, body = (
                "bus_two_stops_away",
                "Bus approaching",
                f"Bus {bus_number} is {away} stop{'s' if away != 1 else ''} away"
                f" (~{eta_min} min).",
            )
        else:
            # Beyond the threshold — silent for now. The threshold can
            # be raised later without schema changes.
            continue

        await _insert_notification(
            supabase_admin,
            user_id=parent_id,
            kind=kind,
            title=title,
            body=body,
            bus_id=bus_id,
            student_id=student.get("student_id"),
            data={**data, "student_id": student.get("student_id")},
        )
        written += 1
        token = await _get_push_token(supabase_admin, parent_id)
        if token:
            await send_expo_push(token, title, body, {"busId": bus_id})

    _logger.info(
        "fan-out: bus=%s stop=%s wrote=%s rows (admins=%s, parents=%s)",
        bus_number,
        stop_name,
        written,
        len(admin_ids),
        sum(1 for s in students if s.get("parent_id")),
    )
    return written
