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
from typing import Any

logger = logging.getLogger(__name__)

TICK_SECONDS = 3.0


async def _tick(supabase_admin) -> None:
    """One simulator pass."""
    buses = (
        supabase_admin.table("buses").select("id, schedule").execute().data
        or []
    )
    for bus in buses:
        schedule: list[dict[str, Any]] = bus.get("schedule") or []
        if not schedule:
            continue

        # Look up the previous step to advance from.
        prev = (
            supabase_admin.table("bus_positions")
            .select("latitude, longitude")
            .eq("bus_id", bus["id"])
            .maybe_single()
            .execute()
            .data
        )

        next_idx = 0
        if prev:
            # Pick the schedule stop whose coords are nearest to the
            # current position. From there advance one step forward.
            def _dist(s: dict[str, Any]) -> float:
                return (s["latitude"] - prev["latitude"]) ** 2 + (
                    s["longitude"] - prev["longitude"]
                ) ** 2

            nearest = min(range(len(schedule)), key=lambda i: _dist(schedule[i]))
            next_idx = (nearest + 1) % len(schedule)

        stop = schedule[next_idx]
        supabase_admin.table("bus_positions").upsert({
            "bus_id": bus["id"],
            "latitude": stop["latitude"],
            "longitude": stop["longitude"],
        }).execute()


async def _run_loop(supabase_admin) -> None:
    """The simulator's main loop. Catches exceptions so a transient DB
    blip doesn't kill the worker."""
    while True:
        try:
            await _tick(supabase_admin)
        except Exception:
            logger.exception("simulator tick failed")
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
