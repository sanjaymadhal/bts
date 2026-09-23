"""Shared pytest fixtures.

Tests override the supabase clients via the FastAPI dependency_overrides
mechanism, so no real Supabase project is required.
"""

from __future__ import annotations

import os
import uuid
from typing import Any
from unittest.mock import MagicMock

# Provide a deterministic POSITION_SECRET so position-auth tests can run
# without a real .env. The matching value is what tests build their
# `X-Position-Secret` header from.
os.environ.setdefault("POSITION_SECRET", "test-position-secret")

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

    `rpc` returns a `FakeQuery` (so `.execute()` chains) that the test
    can pre-load with a return value via `seed_rpc(name, value)`.
    """

    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[dict[str, Any]] = []
        self.rpc_results: dict[str, Any] = {}

    # ---- table seed ------------------------------------------------------

    def seed(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.tables[table] = list(rows)

    def seed_rpc(self, name: str, value: Any) -> None:
        """Configure the value returned by a future `rpc(name).execute()`."""
        self.rpc_results[name] = value

    # ---- table access ----------------------------------------------------

    def table(self, name: str) -> "FakeQuery":
        return FakeQuery(self, name)

    # ---- RPC -------------------------------------------------------------

    def rpc(self, name: str, params: Any = None) -> "FakeRpc":
        """PostgREST `rpc(name, params)`. Returns a builder so the
        caller can chain `.execute()` (matching the real supabase-py
        client). Configure the value with `seed_rpc(name, value)`.
        """
        self.calls.append({"rpc": name, "params": params})
        return FakeRpc(self.rpc_results.get(name))


class FakeRpc:
    def __init__(self, value: Any) -> None:
        self._value = value

    def execute(self) -> "FakeResponse":
        return FakeResponse(self._value)


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

    def gte(self, col: str, val: Any) -> "FakeQuery":
        self._filters.append(("gte", col, val))
        return self

    def in_(self, col: str, vals: list) -> "FakeQuery":
        self._filters.append(("in", col, vals))
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

    def _join_nested(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Emulate PostgREST nested-resource selects like `students(...)`.

        Walks the select string for tokens of the form `tablename(...)`,
        joins the matching rows from the child table, and attaches them
        under a key matching the relationship name. The FK convention
        varies (buses → bus_id, but profiles → school_id) so we try
        a few common parents first.
        """
        import re

        nested = re.findall(r"(\w+)\([^)]*\)", self._select)
        if not nested:
            return rows
        out: list[dict[str, Any]] = []
        for row in rows:
            copy = dict(row)
            for child_table in nested:
                child_rows = list(self.client.tables.get(child_table, []))
                # Try every plausible FK to the parent. Tables ending in `es`
                # (buses, classes) usually singularise to drop both
                # (`bus_id`); other plurals drop one `s` (`schools` →
                # `school_id`). Try a few candidates and let the data
                # pick the right one.
                parent_id = row.get("id")
                fk_candidates = [
                    f"{self.table[:-2]}_id" if self.table.endswith("es") else f"{self.table[:-1]}_id",
                    f"{self.table}_id",
                ]
                joined = []
                for c in child_rows:
                    if any(c.get(fk) == parent_id for fk in fk_candidates):
                        joined.append(c)
                copy[child_table] = joined
            out.append(copy)
        return out

    def execute(self) -> "FakeResponse":
        rows = list(self.client.tables.get(self.table, []))
        for op, col, val in self._filters:
            if op == "eq":
                rows = [r for r in rows if r.get(col) == val]
            elif op == "gte":
                rows = [r for r in rows if (r.get(col) is not None and r.get(col) >= val)]
            elif op == "in":
                rows = [r for r in rows if r.get(col) in val]

        if self._mode == "select":
            joined = self._join_nested(rows)
            result = joined[: self._limit] if self._limit else joined
        elif self._mode == "insert":
            new_rows = self._payload if isinstance(self._payload, list) else [self._payload]
            # Mimic Postgres `gen_random_uuid()` default so tests can
            # rely on `inserted[0]["id"]` like the real DB does. The
            # only tables we rely on having an `id` default in the
            # schema are `buses`, `students`, etc.; the helper does no
            # harm on others.
            for r in new_rows:
                if isinstance(r, dict) and not r.get("id"):
                    r["id"] = str(uuid.uuid4())
                rows.append(r)
            result = new_rows
        elif self._mode == "update":
            for r in rows:
                r.update(self._payload)
            result = rows
        elif self._mode == "delete":
            # Actually remove the rows from the in-memory table so
            # subsequent selects don't see ghosts.
            surviving = [r for r in rows if not r.get("_deleted")]
            result = rows  # deleted rows still reported for tests that assert on it
            rows = surviving
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
        # Persist mutations back to the in-memory table so subsequent
        # queries in tests observe the changes.
        self.client.tables[self.table] = rows
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
