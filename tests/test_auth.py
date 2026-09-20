"""Tests for /auth/* endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _seed_profile(fake_supabase):
    fake_supabase.seed(
        "profiles",
        [
            {
                "id": "user-1",
                "role": "admin",
                "school_id": "school-1",
                "display_name": "Admin Person",
                "phone": "+91 99",
                "linked_student_id": None,
            }
        ],
    )
    fake_supabase.seed(
        "schools",
        [
            {"id": "school-1", "name": "Delhi Public School — East", "code": "DPS-EAST"},
        ],
    )


def test_login_success(client, fake_supabase, auth_header):
    _seed_profile(fake_supabase)

    # Wire the auth() chain on the supabase client.
    auth = MagicMock()
    auth.sign_in_with_password.return_value = MagicMock(
        session=MagicMock(access_token="jwt-token"),
        user=MagicMock(
            id="user-1",
            email="admin@dps-east.test",
            user_metadata={"role": "admin"},
        ),
    )
    fake_supabase.auth = auth

    r = client.post(
        "/auth/login",
        json={"email": "admin@dps-east.test", "password": "x"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"] == "jwt-token"
    assert body["profile"]["role"] == "admin"
    assert body["profile"]["display_name"] == "Admin Person"
    assert body["profile"]["school_name"] == "Delhi Public School — East"


def test_login_bad_credentials_returns_401(client, fake_supabase):
    auth = MagicMock()
    auth.sign_in_with_password.side_effect = Exception("bad")
    fake_supabase.auth = auth
    r = client.post("/auth/login", json={"email": "x@y.z", "password": "nope"})
    assert r.status_code == 401


def test_login_missing_email_returns_422(client):
    r = client.post("/auth/login", json={"password": "x"})
    assert r.status_code == 422


def test_reset_password_always_returns_ok(client, fake_supabase):
    auth = MagicMock()
    auth.reset_password_for_email.return_value = None
    fake_supabase.auth = auth
    r = client.post("/auth/reset-password", json={"email": "a@b.in"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_get_me_returns_profile(client, fake_supabase, auth_header):
    _seed_profile(fake_supabase)
    r = client.get("/auth/me", headers=auth_header(sub="user-1"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "admin"
    assert body["school_name"] == "Delhi Public School — East"


def test_get_me_unauthenticated(client):
    r = client.get("/auth/me")
    assert r.status_code == 401


def test_patch_me_updates_display_name(client, fake_supabase, auth_header):
    _seed_profile(fake_supabase)
    r = client.patch(
        "/auth/me",
        json={"display_name": "Renamed Admin"},
        headers=auth_header(sub="user-1"),
    )
    assert r.status_code == 200, r.text
    # FakeSupabase mutates rows in place; reload.
    rows = fake_supabase.tables["profiles"]
    assert rows[0]["display_name"] == "Renamed Admin"


def test_patch_me_no_op_when_no_fields(client, fake_supabase, auth_header):
    _seed_profile(fake_supabase)
    r = client.patch("/auth/me", json={}, headers=auth_header(sub="user-1"))
    assert r.status_code == 200
    assert fake_supabase.tables["profiles"][0]["display_name"] == "Admin Person"
