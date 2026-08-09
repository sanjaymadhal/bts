"""Shared pytest fixtures.

Tests override the supabase clients via the FastAPI dependency_overrides
mechanism, so no real Supabase project is required.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.deps import get_supabase_admin, get_supabase_user
from app.main import create_app


class FakeSupabase:
    """In-memory stand-in for the supabase client.

    Each table gets a `from_` chain that records the query and returns
    mock data. Tests can pre-seed via `seed(table, rows)` and assert on
    the rows returned by the chain.
    """

    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[dict[str, Any]] = []

    # ---- table seed ------------------------------------------------------

    def seed(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.tables[table] = list(rows)

    # ---- table access ----------------------------------------------------

    def table(self, name: str) -> "FakeQuery":
        return FakeQuery(self, name)


class FakeQuery:
    def __init__(self, client: FakeSupabase, table: str) -> None:
        self.client = client
        self.table = table
        self._filters: list[tuple[str, str, Any]] = []
        self._select = "*"
        self._limit: int | None = None
        self._single = False
        self._mode: str = "select"  # select | insert | update | delete | upsert
        self._payload: Any = None

    # ---- filter builders -------------------------------------------------

    def select(self, cols: str = "*") -> "FakeQuery":
        self._select = cols
        self._mode = "select"
        return self

    def eq(self, col: str, val: Any) -> "FakeQuery":
        self._filters.append(("eq", col, val))
        return self

    def order(self, *_args, **_kw) -> "FakeQuery":
        return self

    def limit(self, n: int) -> "FakeQuery":
        self._limit = n
        return self

    def single(self) -> "FakeQuery":
        self._single = True
        return self

    def maybe_single(self) -> "FakeQuery":
        self._single = True
        return self

    def insert(self, payload: Any) -> "FakeQuery":
        self._mode = "insert"
        self._payload = payload
        return self

    def update(self, payload: Any) -> "FakeQuery":
        self._mode = "update"
        self._payload = payload
        return self

    def upsert(self, payload: Any) -> "FakeQuery":
        self._mode = "upsert"
        self._payload = payload
        return self

    def delete(self) -> "FakeQuery":
        self._mode = "delete"
        return self

    # ---- terminal --------------------------------------------------------

    def execute(self) -> "FakeResponse":
        rows = list(self.client.tables.get(self.table, []))
        for op, col, val in self._filters:
            if op == "eq":
                rows = [r for r in rows if r.get(col) == val]

        if self._mode == "select":
            result = rows[: self._limit] if self._limit else rows
        elif self._mode == "insert":
            new_rows = self._payload if isinstance(self._payload, list) else [self._payload]
            for r in new_rows:
                rows.append(r)
            result = new_rows
        elif self._mode == "update":
            for r in rows:
                r.update(self._payload)
            result = rows
        elif self._mode == "delete":
            for r in rows:
                r["_deleted"] = True
            result = rows
        elif self._mode == "upsert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            for p in payloads:
                idx = next(
                    (i for i, r in enumerate(rows) if r.get("id") == p.get("id")),
                    None,
                )
                if idx is None:
                    rows.append(p)
                else:
                    rows[idx].update(p)
            result = payloads
        else:
            result = []

        if self._single:
            result = result[0] if result else None

        self.client.calls.append(
            {
                "table": self.table,
                "mode": self._mode,
                "filters": list(self._filters),
                "payload": self._payload,
            }
        )
        return FakeResponse(result)


class FakeResponse:
    def __init__(self, data: Any) -> None:
        self.data = data


@pytest.fixture
def fake_supabase() -> FakeSupabase:
    return FakeSupabase()


@pytest.fixture
def fake_admin(fake_supabase: FakeSupabase) -> FakeSupabase:
    return fake_supabase


@pytest.fixture
def fake_user(fake_supabase: FakeSupabase) -> FakeSupabase:
    return fake_supabase


@pytest.fixture
def app(fake_user: FakeSupabase, fake_admin: FakeSupabase):
    application = create_app()
    application.dependency_overrides[get_supabase_user] = lambda: fake_user
    application.dependency_overrides[get_supabase_admin] = lambda: fake_admin
    return application


@pytest.fixture
def client(app) -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def auth_header():
    """Build an Authorization header from a forged JWT.

    Uses the settings' SUPABASE_JWT_SECRET so the real validator accepts it.
    """

    from jose import jwt
    from app.config import get_settings

    secret = get_settings().SUPABASE_JWT_SECRET

    def _make(sub: str = "user-1", email: str = "u@test.in", role: str = "authenticated") -> dict[str, str]:
        token = jwt.encode(
            {"sub": sub, "email": email, "role": role},
            secret,
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}"}

    return _make
