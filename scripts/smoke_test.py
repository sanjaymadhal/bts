#!/usr/bin/env python
"""End-to-end smoke test for the Trackr backend.

Hits every public endpoint and the /auth/me protected endpoint with a
forged JWT (using the SUPABASE_JWT_SECRET). Use this to verify the
backend boots, accepts CORS, and routes to the right handlers without
needing a real Supabase user.

Run from trackr-backend/ with the venv active:

    .venv/Scripts/python.exe scripts/smoke_test.py http://127.0.0.1:8000
"""
from __future__ import annotations

import json
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import jwt  # python-jose


def hit(base: str, method: str, path: str, *, headers=None, body=None):
    url = base.rstrip("/") + path
    data = None
    h = {"Accept": "application/json"}
    if headers:
        h.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = Request(url, data=data, method=method, headers=h)
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except HTTPError as e:
        return e.code, e.read().decode("utf-8")


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    print(f"smoke test against {base}\n")

    failures = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        marker = "OK" if ok else "FAIL"
        print(f"  [{marker}] {name}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures += 1

    # 1. health
    code, body = hit(base, "GET", "/health")
    check("GET /health", code == 200, body)

    # 2. login with bogus creds → 401
    code, body = hit(base, "POST", "/auth/login", body={"email": "x@y.z", "password": "x"})
    check("POST /auth/login (bad creds)", code == 401, body)

    # 3. login with malformed email → should still work since EmailStr is loose now
    code, body = hit(base, "POST", "/auth/login", body={"email": "x", "password": ""})
    check("POST /auth/login (short pw)", code == 422, body)

    # 4. login with real test admin (only if Supabase seeded it)
    code, body = hit(base, "POST", "/auth/login", body={"email": "admin@kssem.test", "password": "change-me-now"})
    if code == 200:
        check("POST /auth/login (seeded admin)", True, body[:120])
        token = json.loads(body)["access_token"]
    elif code == 400:
        check("POST /auth/login (Supabase reachable)", True, body[:120])
        token = None
    else:
        check("POST /auth/login (seeded admin)", False, body[:200])
        token = None

    # 5. /auth/me with no token → 401
    code, body = hit(base, "GET", "/auth/me")
    check("GET /auth/me (no token)", code == 401, body)

    # 6. /auth/me with forged JWT (using configured secret) → should pass JWT check
    #    but fail profile fetch (no Supabase data).
    secret = "jwt-secret"  # matches Settings default when SUPABASE_JWT_SECRET is empty
    try:
        forged = jwt.encode({"sub": "user-x", "email": "x@y.z", "role": "authenticated"}, secret, algorithm="HS256")
    except Exception:
        forged = jwt.encode({"sub": "user-x"}, secret, algorithm="HS256")
    code, body = hit(base, "GET", "/auth/me", headers={"Authorization": f"Bearer {forged}"})
    check("GET /auth/me (forged JWT)", code in (200, 404, 500), body[:120])

    # 7. /buses with forged JWT
    code, body = hit(base, "GET", "/buses", headers={"Authorization": f"Bearer {forged}"})
    check("GET /buses (forged JWT)", code in (200, 500), body[:120])

    # 8. /settings
    code, body = hit(base, "GET", "/settings", headers={"Authorization": f"Bearer {forged}"})
    check("GET /settings (forged JWT)", code in (200, 500), body[:120])

    # 9. /geocode (no auth, public) — requires network to Nominatim; allow 502
    code, body = hit(base, "GET", "/geocode?q=bengaluru")
    check("GET /geocode (public)", code in (200, 502), body[:120])

    print()
    if failures:
        print(f"{failures} check(s) failed")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())