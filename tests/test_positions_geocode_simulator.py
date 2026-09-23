"""Tests for positions, geocode, and simulator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---- /positions ------------------------------------------------------------


def test_positions_requires_service_role(client):
    # Shared-secret auth: missing header → 403 (the request reached the
    # auth check but couldn't satisfy it). The simulator/driver app
    # always sends X-Position-Secret, so 401-without-body is reserved
    # for future JWT auth.
    r = client.post("/positions/bus-1", json={"latitude": 12.97, "longitude": 77.59})
    assert r.status_code == 403


def test_positions_wrong_token_rejected(client):
    r = client.post(
        "/positions/bus-1",
        json={"latitude": 12.97, "longitude": 77.59},
        headers={"X-Position-Secret": "wrong-token"},
    )
    assert r.status_code == 403


def test_positions_service_role_upserts(client, fake_supabase):
    from app.config import get_settings

    secret = get_settings().POSITION_SECRET
    r = client.post(
        "/positions/bus-1",
        json={"latitude": 12.97, "longitude": 77.59},
        headers={"X-Position-Secret": secret},
    )
    assert r.status_code == 204
    rows = fake_supabase.tables.get("bus_positions", [])
    assert any(row["bus_id"] == "bus-1" for row in rows)


def test_positions_list_defaults_online_when_no_timestamp(client, fake_supabase, auth_header):
    fake_supabase.seed(
        "bus_positions",
        [{"bus_id": "bus-1", "latitude": 12.97, "longitude": 77.59,
          "speed": 10, "altitude": 0, "updated_at": None}],
    )
    r = client.get("/positions", headers=auth_header())
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["is_online"] is False  # no timestamp → not online


def test_positions_list_marks_stale_offline(client, fake_supabase, auth_header):
    fake_supabase.seed(
        "bus_positions",
        [{"bus_id": "bus-1", "latitude": 12.97, "longitude": 77.59,
          "speed": 10, "altitude": 0, "updated_at": "2020-01-01T00:00:00Z"}],
    )
    r = client.get("/positions", headers=auth_header())
    assert r.status_code == 200
    assert r.json()[0]["is_online"] is False


# ---- /geocode --------------------------------------------------------------


@pytest.mark.asyncio
async def test_geocode_returns_normalised_results(monkeypatch):
    from app import geocode

    # Reset module-level cache between tests so we always hit the mock.
    geocode._CACHE.clear()

    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = [
        {"lat": "12.97", "lon": "77.59", "display_name": "Indiranagar, Bengaluru"},
        {"lat": "12.95", "lon": "77.62", "display_name": "Koramangala, Bengaluru"},
    ]

    fake_client = AsyncMock()
    fake_client.__aenter__.return_value.get = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(geocode.httpx, "AsyncClient", lambda **kw: fake_client)

    results = await geocode._geocode("indiranagar")
    assert len(results) == 2
    assert results[0]["lat"] == 12.97


@pytest.mark.asyncio
async def test_geocode_cache_hit_skips_http(monkeypatch):
    from app import geocode

    geocode._CACHE.clear()
    geocode._CACHE["indiranagar"] = [{"name": "x", "lat": 1.0, "lng": 2.0, "address": "x"}]

    def boom(*_a, **_kw):
        raise AssertionError("http should not be called on cache hit")

    monkeypatch.setattr(geocode.httpx, "AsyncClient", boom)

    results = await geocode._geocode("Indiranagar")  # different case → normalised same
    assert results == [{"name": "x", "lat": 1.0, "lng": 2.0, "address": "x"}]


def test_geocode_endpoint_rejects_short_query(client):
    r = client.get("/geocode?q=ab")
    assert r.status_code == 422


# ---- simulator -------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_advances_one_step(fake_supabase):
    from app import simulator

    fake_supabase.seed(
        "buses",
        [
            {
                "id": "bus-1",
                "schedule": [
                    {"time": "07:00", "name": "A", "latitude": 12.97, "longitude": 77.59},
                    {"time": "07:30", "name": "B", "latitude": 12.95, "longitude": 77.62},
                ],
            }
        ],
    )

    await simulator._tick(fake_supabase)
    pos = fake_supabase.tables["bus_positions"][0]
    # No previous position → defaults to idx 0.
    assert pos["latitude"] == 12.97
    assert pos["longitude"] == 77.59

    # Run a second tick — should advance to idx 1.
    await simulator._tick(fake_supabase)
    pos = fake_supabase.tables["bus_positions"][0]
    assert pos["latitude"] == 12.95


@pytest.mark.asyncio
async def test_tick_wraps_when_nearest_is_last(fake_supabase):
    from app import simulator

    fake_supabase.seed(
        "buses",
        [
            {
                "id": "bus-1",
                "schedule": [
                    {"time": "07:00", "name": "A", "latitude": 0.0, "longitude": 0.0},
                    {"time": "07:30", "name": "B", "latitude": 1.0, "longitude": 1.0},
                ],
            }
        ],
    )
    # Position is at B — nearest is idx 1, next wraps to 0.
    fake_supabase.seed(
        "bus_positions",
        [{"bus_id": "bus-1", "latitude": 1.0, "longitude": 1.0, "updated_at": None}],
    )
    await simulator._tick(fake_supabase)
    pos = fake_supabase.tables["bus_positions"][0]
    assert pos["latitude"] == 0.0


@pytest.mark.asyncio
async def test_tick_skips_buses_with_no_schedule(fake_supabase):
    from app import simulator

    fake_supabase.seed(
        "buses",
        [{"id": "bus-1", "schedule": []}],
    )
    await simulator._tick(fake_supabase)
    assert "bus_positions" not in fake_supabase.tables or not fake_supabase.tables["bus_positions"]
