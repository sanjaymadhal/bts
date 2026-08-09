"""Geocoding proxy backed by OpenStreetMap Nominatim.

Per backend spec §6:
- Single in-flight queue (one HTTP request at a time).
- TTL cache (30 days).
- 3-char minimum query.
- Backend-only User-Agent required by Nominatim's TOS.
- Top 5 results.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from cachetools import TTLCache
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

router = APIRouter()


# ---------------------------------------------------------------------------
# Cache + queue
# ---------------------------------------------------------------------------


_CACHE: TTLCache[str, list[dict[str, Any]]] = TTLCache(maxsize=1024, ttl=30 * 24 * 3600)
_QUEUE_LOCK = asyncio.Lock()

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "trackr-backend/1.0 (ops@trackr.app)"


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
    """Single in-flight HTTP call to Nominatim.

    Cache misses serialise through `_QUEUE_LOCK` to honour the 1 req/s
    rate-limit. Cache hits short-circuit before the lock.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            NOMINATIM_URL,
            params={"format": "json", "q": q, "limit": 5},
            headers={"User-Agent": USER_AGENT},
        )
        if resp.status_code != 200:
            return []
        out: list[dict[str, Any]] = []
        for r in resp.json():
            try:
                lat = float(r["lat"])
                lng = float(r["lon"])
            except (KeyError, ValueError):
                continue
            out.append({
                "name": r.get("display_name", ""),
                "lat": lat,
                "lng": lng,
                "address": r.get("display_name", ""),
            })
        return out


async def _geocode(q: str) -> list[dict[str, Any]]:
    """Serialised, cached Nominatim call."""
    key = _normalize(q)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    async with _QUEUE_LOCK:
        # Double-check inside the lock in case another caller already
        # populated the cache while we waited.
        cached = _CACHE.get(key)
        if cached is not None:
            return cached
        results = await _fetch_nominatim(q)
        # Cache even empty results — a 0-result lookup shouldn't be re-hit
        # on every keystroke.
        _CACHE[key] = results
        # Honor the 1 req/s limit between consecutive misses.
        await asyncio.sleep(1.0)
        return results


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
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Geocoding service unavailable.",
        )
    return [GeocodeResult(**r) for r in results]
