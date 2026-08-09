"""Tests for /students and /invitations and /settings."""

from __future__ import annotations

from unittest.mock import MagicMock


# ---- /students -------------------------------------------------------------


def test_list_students(client, fake_supabase, auth_header):
    fake_supabase.seed(
        "students",
        [
            {"id": "s1", "bus_id": "bus-1", "name": "Aarav",
             "class_grade": "6 — B", "stop": "Indiranagar"},
        ],
    )
    r = client.get("/students", headers=auth_header())
    assert r.status_code == 200
    assert r.json()[0]["name"] == "Aarav"


def test_create_student_returns_row(client, fake_supabase, auth_header):
    r = client.post(
        "/students",
        headers=auth_header(),
        json={"name": "Saanvi", "class_grade": "5 — A", "stop": "Koramangala"},
    )
    assert r.status_code == 201
    assert r.json()["name"] == "Saanvi"
    assert fake_supabase.tables["students"][0]["name"] == "Saanvi"


def test_assign_student_to_bus(client, fake_supabase, auth_header):
    fake_supabase.seed(
        "students",
        [{"id": "s1", "bus_id": None, "name": "X",
          "class_grade": "5 — A", "stop": "Y"}],
    )
    r = client.post(
        "/students/s1/assign",
        headers=auth_header(),
        json={"bus_id": "bus-1"},
    )
    assert r.status_code == 200
    assert fake_supabase.tables["students"][0]["bus_id"] == "bus-1"


def test_assign_student_to_null_unassigns(client, fake_supabase, auth_header):
    fake_supabase.seed(
        "students",
        [{"id": "s1", "bus_id": "bus-1", "name": "X",
          "class_grade": "5 — A", "stop": "Y"}],
    )
    r = client.post(
        "/students/s1/assign",
        headers=auth_header(),
        json={"bus_id": None},
    )
    assert r.status_code == 200
    assert fake_supabase.tables["students"][0]["bus_id"] is None


def test_delete_student_returns_204(client, fake_supabase, auth_header):
    fake_supabase.seed(
        "students",
        [{"id": "s1", "bus_id": None, "name": "X",
          "class_grade": "5 — A", "stop": "Y"}],
    )
    r = client.delete("/students/s1", headers=auth_header())
    assert r.status_code == 204


# ---- /invitations ----------------------------------------------------------


def test_create_invitation_writes_rows(client, fake_supabase, auth_header):
    fake_supabase.seed("schools", [{"id": "school-1", "name": "DPS East", "code": "DPS-EAST"}])

    admin = MagicMock()
    admin.auth.admin.create_user.return_value = MagicMock(
        user=MagicMock(id="auth-user-1")
    )
    fake_supabase.auth = MagicMock()
    # For profile lookup after we use supabase_user too — they share tables.

    r = client.post(
        "/invitations",
        headers=auth_header(),
        json={"email": "parent@dps.in", "role": "parent",
              "display_name": "Priya Sharma"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == "parent@dps.in"
    assert body["role"] == "parent"
    # Invitation row landed in the table.
    assert any(i["email"] == "parent@dps.in" for i in fake_supabase.tables.get("invitations", []))


def test_create_invitation_requires_auth(client):
    r = client.post(
        "/invitations",
        json={"email": "x@y.z", "role": "parent", "display_name": "X"},
    )
    assert r.status_code == 401


# ---- /settings -------------------------------------------------------------


def _seed_settings_world(fake_supabase):
    fake_supabase.seed(
        "schools",
        [{"id": "school-1", "name": "DPS East", "code": "DPS-EAST"}],
    )
    fake_supabase.seed(
        "school_settings",
        [{"school_id": "school-1", "notifications_enabled": True,
          "auto_assign_stops": False, "language": "en",
          "default_alert_radius_m": 250}],
    )


def test_get_settings(client, fake_supabase, auth_header):
    _seed_settings_world(fake_supabase)
    r = client.get("/settings", headers=auth_header())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["school_code"] == "DPS-EAST"
    assert body["school_name"] == "DPS East"
    assert body["language"] == "en"


def test_patch_settings_updates_partial(client, fake_supabase, auth_header):
    _seed_settings_world(fake_supabase)
    r = client.patch(
        "/settings",
        headers=auth_header(),
        json={"auto_assign_stops": True, "school_name": "DPS East (New)"},
    )
    assert r.status_code == 200, r.text
    # school_name on schools, auto_assign_stops on school_settings.
    assert fake_supabase.tables["schools"][0]["name"] == "DPS East (New)"
    assert fake_supabase.tables["school_settings"][0]["auto_assign_stops"] is True
    # Untouched fields stay.
    assert fake_supabase.tables["school_settings"][0]["language"] == "en"


def test_patch_settings_validates_language(client, fake_supabase, auth_header):
    _seed_settings_world(fake_supabase)
    r = client.patch(
        "/settings",
        headers=auth_header(),
        json={"language": "zz"},
    )
    assert r.status_code == 422
