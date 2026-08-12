"""Driver simulator.

Every `TICK_SECONDS` the simulator advances each active bus one step
along its `schedule`. The motion is circular: after the last stop we
wrap back to the first. UPSERT into `bus_positions` via the service-role
client triggers Supabase Realtime broadcasts to subscribed mobile clients.

Started in the FastAPI lifespan if `SIMULATE_DRIVERS=true`.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

from .notification_engine import emit_stop_transition

logger = logging.getLogger(__name__)

TICK_SECONDS = 3.0
# Backoff if Supabase is unreachable. Caps the spam to ~once per 30s
# while still letting transient DNS / network blips recover quickly.
BACKOFF_INITIAL = 3.0
BACKOFF_MAX = 30.0

#: Module-level cache of which schedule index each bus was last seen at.
#: Used by `_tick` to fire `emit_stop_transition` only on actual stop
#: transitions rather than every tick (which would spam admins on every
#: position upsert). `None` means "first observation for this bus".
_last_stop_idx: dict[str, int | None] = {}


async def _tick(supabase_admin) -> None:
    """One simulator pass. The supabase client is synchronous, so each
    network call is wrapped in `run_in_executor` to keep the FastAPI
    event loop responsive while DNS / TLS / POST are in flight.
    """
    loop = asyncio.get_running_loop()

    def _select_buses():
        res = supabase_admin.table("buses").select("id, number, schedule").execute()
        data = None if res is None else getattr(res, "data", None)
        return data or []

    buses = await loop.run_in_executor(None, _select_buses)

    for bus in buses:
        # `schedule` is a jsonb column. Supabase-py returns it as a
        # list of dicts, but a freshly-migrated or hand-edited row
        # could leave it as a string, a list of strings (rare), or
        # null. Coerce defensively so a malformed row logs and skips
        # instead of crashing the whole tick with AttributeError /
        # TypeError on `s["latitude"]`.
        schedule = _coerce_schedule(bus.get("schedule"))
        if not schedule:
            continue

        def _select_prev(bus_id=bus["id"]):
            res = (
                supabase_admin.table("bus_positions")
                .select("latitude, longitude")
                .eq("bus_id", bus_id)
                .maybe_single()
                .execute()
            )
            return None if res is None else getattr(res, "data", None)

        prev = await loop.run_in_executor(None, _select_prev)

        next_idx = 0
        if prev and all(k in prev for k in ("latitude", "longitude")):
            # Pick the schedule stop whose coords are nearest to the
            # current position. From there advance one step forward.
            def _dist(s: dict[str, Any]) -> float:
                try:
                    return (s["latitude"] - prev["latitude"]) ** 2 + (
                        s["longitude"] - prev["longitude"]
                    ) ** 2
                except (TypeError, KeyError):
                    return float("inf")

            nearest = min(range(len(schedule)), key=lambda i: _dist(schedule[i]))
            next_idx = (nearest + 1) % len(schedule)

        stop = schedule[next_idx]
        # Guard the actual upsert so a half-formed stop can't crash us.
        try:
            lat = float(stop["latitude"])
            lng = float(stop["longitude"])
        except (KeyError, TypeError, ValueError):
            logger.debug("simulator: skipping malformed stop %r on bus %s", stop, bus.get("id"))
            continue

        def _upsert(bus_id=bus["id"], lat=lat, lng=lng):
            supabase_admin.table("bus_positions").upsert(
                {"bus_id": bus_id, "latitude": lat, "longitude": lng}
            ).execute()

        await loop.run_in_executor(None, _upsert)

        # Notification hook — fire only when the bus actually changed
        # stops (not on every position upsert, which would spam admins).
        # On the first observation we always fire so a freshly-booted
        # simulator tells the school where each bus currently is.
        bus_id = bus["id"]
        bus_number = bus.get("number") or ""
        stop_name = (stop.get("name") or stop.get("stop_name") or "").strip() or "(stop)"
        prev_idx = _last_stop_idx.get(bus_id)
        if prev_idx != next_idx:
            asyncio.create_task(
                _safe_emit_stop_transition(
                    supabase_admin,
                    bus_id=bus_id,
                    bus_number=bus_number,
                    stop_index=next_idx,
                    stop_name=stop_name,
                )
            )
            _last_stop_idx[bus_id] = next_idx


async def _safe_emit_stop_transition(
    supabase_admin,
    *,
    bus_id: str,
    bus_number: str,
    stop_index: int,
    stop_name: str,
) -> None:
    """Wrap `emit_stop_transition` so a fan-out failure never bubbles up.

    The simulator's tick loop runs `_tick` inside its own broad
    try/except; we add an extra layer here because the fan-out runs as
    an `asyncio.create_task` and an unhandled exception in a Task is
    silently logged by asyncio (and easy to miss in dev).
    """
    try:
        await emit_stop_transition(
            supabase_admin,
            bus_id=bus_id,
            bus_number=bus_number,
            stop_index=stop_index,
            stop_name=stop_name,
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "stop-transition emit failed for bus=%s stop=%s: %s",
            bus_id,
            stop_name,
            exc,
        )


def _coerce_schedule(raw: Any) -> list[dict[str, Any]]:
    """Normalise whatever the DB hands back into `list[dict]`.

    Handles the three shapes seen in the wild:
      - the canonical list-of-dict shape,
      - a JSON-encoded string (older migrations / select projections
        that don't expand jsonb), and
      - None / missing.

    Filters out items that aren't dicts with the expected keys so a
    partially-populated row doesn't AttributeError the tick.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for s in raw:
        if isinstance(s, dict) and "latitude" in s and "longitude" in s:
            out.append(s)
    return out


async def _run_loop(supabase_admin) -> None:
    """The simulator's main loop. Catches exceptions so a transient DB
    blip doesn't kill the worker. Exponential backoff keeps the log
    readable when Supabase is down."""
    backoff = BACKOFF_INITIAL
    while True:
        try:
            await _tick(supabase_admin)
            backoff = BACKOFF_INITIAL  # reset on success
        except Exception as exc:
            logger.warning(
                "simulator tick failed (retrying in %.0fs): %s: %s",
                backoff,
                exc.__class__.__name__,
                exc,
            )
            logger.debug("simulator tick traceback", exc_info=exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue
        await asyncio.sleep(TICK_SECONDS)


async def start_simulator() -> asyncio.Task:
    """Spawn the background task. Returns the task handle so the lifespan
    can cancel it on shutdown.

    We construct the service-role Supabase client here directly — the
    dependency-injected version is tied to a request scope, and the
    simulator lives for the whole process lifetime.
    """
    from supabase import create_client

    from .config import get_settings

    settings = get_settings()
    # Resolve the Supabase host once at startup so a DNS hiccup doesn't
    # race with the very first tick. If the host can't be resolved yet
    # we log it and let the loop's backoff recover.
    try:
        info = socket.getaddrinfo(settings.SUPABASE_URL, 443, type=socket.SOCK_STREAM)
        ips = sorted({addr[4][0] for addr in info})
        logger.info("supabase %s resolves to %s", settings.SUPABASE_URL, ips)
    except socket.gaierror as exc:
        logger.warning(
            "supabase DNS not ready yet (%s); simulator will retry", exc
        )

    supabase_admin = create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_SERVICE_ROLE_KEY,
    )
    task = asyncio.create_task(_run_loop(supabase_admin))
    logger.info("driver simulator started")
    return task


async def stop_simulator(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    logger.info("driver simulator stopped")
