"""Geocoding proxy backed by OpenStreetMap Nominatim.

Per backend spec §6:
- Single in-flight queue (one HTTP request at a time).
- TTL cache (30 days).
- 3-char minimum query.
- Backend-only User-Agent required by Nominatim's TOS.
- Top 5 results.

Implementation:
- Cache hits short-circuit (no rate-limit hit).
- Misses go through a single-worker async queue so the 1 req/s
  rate-limit applies to actual fetches only. Multiple callers waiting
  on the same key share one Future.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

import httpx
from cachetools import TTLCache
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

router = APIRouter()


# ---------------------------------------------------------------------------
# Cache + worker queue
# ---------------------------------------------------------------------------


_CACHE: TTLCache[str, list[dict[str, Any]]] = TTLCache(maxsize=1024, ttl=30 * 24 * 3600)
# In-flight lookups: key -> Future that resolves with the results.
# Lets concurrent callers for the same query share one HTTP request.
_INFLIGHT: dict[str, asyncio.Future[list[dict[str, Any]]]] = {}
# Single-worker task that drains the miss queue one at a time.
_WORKER: asyncio.Task | None = None
_MISS_QUEUE: asyncio.Queue[str] | None = None

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "trackr-backend/1.0 (ops@trackr.app)"
NOMINATIM_GAP_SECONDS = 1.0


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class GeocodeResult(BaseModel):
    name: str
    lat: float
    lng: float
    address: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize(q: str) -> str:
    return " ".join(q.lower().split())


async def _fetch_nominatim(q: str) -> list[dict[str, Any]]:
    """One HTTP call to Nominatim. Never raises; returns [] on failure."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(
                NOMINATIM_URL,
                params={"format": "json", "q": q, "limit": 5},
                headers={"User-Agent": USER_AGENT},
            )
        except httpx.HTTPError:
            return []
        if resp.status_code != 200:
            return []
        out: list[dict[str, Any]] = []
        for r in resp.json():
            try:
                lat = float(r["lat"])
                lng = float(r["lon"])
            except (KeyError, ValueError, TypeError):
                continue
            out.append({
                "name": r.get("display_name", ""),
                "lat": lat,
                "lng": lng,
                "address": r.get("display_name", ""),
            })
        return out


async def _worker_loop() -> None:
    """Single worker that drains the miss queue, honoring 1 req/s."""
    assert _MISS_QUEUE is not None
    while True:
        key = await _MISS_QUEUE.get()
        # Coalesce: if 5 callers queued the same key, only fetch once.
        future = _INFLIGHT.get(key)
        if future is None or future.done():
            continue
        try:
            await asyncio.sleep(NOMINATIM_GAP_SECONDS)  # rate-limit
            results = await _fetch_nominatim(key)
            _CACHE[key] = results
            future.set_result(results)
        except Exception as exc:  # pragma: no cover
            if not future.done():
                future.set_exception(exc)
        finally:
            _INFLIGHT.pop(key, None)
            _MISS_QUEUE.task_done()


async def _ensure_worker() -> None:
    global _WORKER, _MISS_QUEUE
    if _WORKER is None or _WORKER.done():
        _MISS_QUEUE = asyncio.Queue()
        _WORKER = asyncio.create_task(_worker_loop())


async def _geocode(q: str) -> list[dict[str, Any]]:
    """Cached + rate-limited Nominatim lookup."""
    key = _normalize(q)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    # Coalesce concurrent lookups for the same key on a single Future.
    future = _INFLIGHT.get(key)
    if future is None:
        await _ensure_worker()
        future = asyncio.get_running_loop().create_future()
        _INFLIGHT[key] = future
        assert _MISS_QUEUE is not None
        await _MISS_QUEUE.put(key)
    return await future


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("/geocode", response_model=list[GeocodeResult])
async def geocode(q: Annotated[str, Query(min_length=3)]) -> list[GeocodeResult]:
    """Top-5 Nominatim matches for a 3+ char query.

    Empty result is a valid 200 response (caller shows "no matches").
    """
    try:
        results = await _geocode(q)
    except httpx.HTTPError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Geocoding service unavailable.",
        )
    return [GeocodeResult(**r) for r in results]