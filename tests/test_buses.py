"""Tests for /profiles and /buses endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ---- /profiles -------------------------------------------------------------


def _seed_auth_client(client, fake_supabase, *, sub="user-1"):
    """Mount a working /auth/me lookup so client.get('/auth/me') works."""
    fake_supabase.seed(
        "profiles",
        [{"id": sub, "role": "admin", "school_id": "school-1",
          "display_name": "Admin", "phone": None, "linked_student_id": None}],
    )
    # SCHOOL_CODE must match the default in app.config.Settings so the
    # school lookup that gates profile visibility finds a row.
    fake_supabase.seed("schools", [{"id": "school-1", "name": "KSSEM", "code": "KSSEM"}])


def test_list_drivers(client, fake_supabase, auth_header):
    _seed_auth_client(client, fake_supabase)
    fake_supabase.seed(
        "profiles",
        fake_supabase.tables.get("profiles", []) + [
            {"id": "driver-1", "role": "driver", "school_id": "school-1",
             "display_name": "Ravi Kumar", "phone": "+91 99"},
        ],
    )
    r = client.get("/profiles?role=driver", headers=auth_header())
    assert r.status_code == 200
    assert r.json() == [
        {"id": "driver-1", "display_name": "Ravi Kumar", "phone": "+91 99"}
    ]


def test_list_profiles_requires_auth(client):
    r = client.get("/profiles?role=driver")
    assert r.status_code == 401


# ---- /buses ----------------------------------------------------------------


def _seed_bus(supabase, bus_id="bus-1", **overrides):
    base = {
        "id": bus_id,
        "number": "KA-05-1234",
        "route": "Route 12",
        "capacity": 40,
        "driver_id": None,
        "schedule": [
            {"time": "07:15", "name": "Start", "latitude": 12.97, "longitude": 77.59},
        ],
    }
    base.update(overrides)
    supabase.seed("buses", [base])


def test_list_buses_returns_each_with_students(client, fake_supabase, auth_header):
    _seed_bus(fake_supabase, bus_id="bus-1")
    fake_supabase.seed(
        "students",
        [
            {"id": "s1", "name": "Aarav", "class_grade": "6 — B", "stop": "X", "bus_id": "bus-1"},
        ],
    )
    r = client.get("/buses", headers=auth_header())
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    assert body[0]["number"] == "KA-05-1234"
    assert body[0]["students"][0]["name"] == "Aarav"


def test_get_bus_returns_full_payload(client, fake_supabase, auth_header):
    _seed_bus(fake_supabase)
    r = client.get("/buses/bus-1", headers=auth_header())
    assert r.status_code == 200
    body = r.json()
    assert body["schedule"][0]["time"] == "07:15"
    assert body["students"] == []


def test_get_bus_missing_returns_404(client, fake_supabase, auth_header):
    _seed_bus(fake_supabase)
    r = client.get("/buses/nope", headers=auth_header())
    assert r.status_code == 404


def test_create_bus_inserts_and_returns(client, fake_supabase, auth_header):
    r = client.post(
        "/buses",
        headers=auth_header(),
        json={
            "number": "KA-05-9999",
            "route": "Route 1",
            "capacity": 30,
            "schedule": [
                {"time": "07:00", "name": "Origin", "latitude": 12.9, "longitude": 77.6},
                {"time": "07:30", "name": "School", "latitude": 12.95, "longitude": 77.62},
            ],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["number"] == "KA-05-9999"
    assert len(body["schedule"]) == 2
    # Bus is in the table now.
    assert fake_supabase.tables["buses"][0]["number"] == "KA-05-9999"


def test_patch_bus_updates_partial(client, fake_supabase, auth_header):
    _seed_bus(fake_supabase)
    r = client.patch(
        "/buses/bus-1",
        headers=auth_header(),
        json={"capacity": 50},
    )
    assert r.status_code == 200, r.text
    assert fake_supabase.tables["buses"][0]["capacity"] == 50


def test_delete_bus_returns_204(client, fake_supabase, auth_header):
    _seed_bus(fake_supabase)
    r = client.delete("/buses/bus-1", headers=auth_header())
    assert r.status_code == 204


def test_bus_position_returns_latest(client, fake_supabase, auth_header):
    _seed_bus(fake_supabase)
    fake_supabase.seed(
        "bus_positions",
        [{"bus_id": "bus-1", "latitude": 12.97, "longitude": 77.59,
          "updated_at": "2026-08-09T10:00:00Z"}],
    )
    r = client.get("/buses/bus-1/position", headers=auth_header())
    assert r.status_code == 200
    body = r.json()
    assert body["latitude"] == 12.97


def test_bus_position_404_when_no_row(client, fake_supabase, auth_header):
    _seed_bus(fake_supabase)
    r = client.get("/buses/bus-1/position", headers=auth_header())
    assert r.status_code == 404


def test_bus_endpoints_require_auth(client):
    r = client.get("/buses")
    assert r.status_code == 401
    r = client.get("/buses/x")
    assert r.status_code == 401
    r = client.post("/buses", json={"number": "x", "route": "y", "capacity": 1})
    assert r.status_code == 401
