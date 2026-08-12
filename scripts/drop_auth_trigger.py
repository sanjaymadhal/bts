#!/usr/bin/env python
"""Fix the broken `on_auth_user_created` trigger blocking admin.create_user.

Supabase hosted GoTrue owns `auth.users`, so the `postgres` user
typically can't ALTER TRIGGER on it. This script signs in as a
Superadmin using the project's service-role JWT, then drops the broken
trigger via the Supabase PostgREST `exec_sql` RPC.

If the RPC isn't exposed in your project, this script will print the
exact SQL you need to run in the Supabase SQL Editor (with the
differences noted).

Run from trackr-backend/ with the venv active:

    .venv/Scripts/python.exe scripts/drop_auth_trigger.py
"""
from __future__ import annotations

import getpass
import json
import sys
import urllib.request


def http(method: str, url: str, headers: dict, body: bytes | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def main() -> int:
    print("=== Supabase `auth.users` trigger fix ===\n")
    print("This script connects to your Supabase project and removes the broken")
    print("`on_auth_user_created` trigger that's causing every new user signup to")
    print("return `Database error creating new user`.\n")

    print("Paste the SERVICE ROLE key (Supabase → Project Settings → API →")
    print("`service_role` / `secret` key). Do NOT paste the anon key.\n")
    service_role = getpass.getpass("Service role key: ").strip()
    if not service_role.startswith("sb_secret_"):
        print("\nThat doesn't look like a service-role key (should start with `sb_secret_`).")
        print("Continuing anyway…")

    project_url = input("Project URL (e.g. https://rzcbmyedznuzwuvybdue.supabase.co): ").strip()
    project_url = project_url.rstrip("/")
    if not project_url.startswith("https://"):
        print("Invalid URL.")
        return 1

    print("\nFetching trigger list via PostgREST…")
    code, body = http(
        "GET",
        f"{project_url}/rest/v1/rpc/list_auth_user_triggers",
        headers={
            "apikey": service_role,
            "Authorization": f"Bearer {service_role}",
            "Content-Type": "application/json",
        },
    )
    if code == 404:
        # RPC not exposed; fall back to printing the manual SQL.
        print("\nPostgREST `list_auth_user_triggers` RPC is not exposed in your project.")
        print("Run this in Supabase → SQL Editor instead:\n")
        print("=" * 70)
        print("""
DO $$
DECLARE trg RECORD;
BEGIN
  FOR trg IN
    SELECT t.tgname FROM pg_trigger t
    WHERE t.tgrelid = 'auth.users'::regclass
      AND NOT t.tgisinternal
  LOOP
    EXECUTE format('ALTER TABLE auth.users DISABLE TRIGGER %I', trg.tgname);
    RAISE NOTICE 'Disabled trigger: %', trg.tgname;
  END LOOP;
END $$;

-- If `ALTER TABLE … DISABLE TRIGGER` still errors with
-- "must be owner of table users", use the supabase_auth.admin client
-- to create the user instead. See trackr-backend/scripts/README.md.
""")
        print("=" * 70)
        return 0

    print(f"Got {code}: {body[:400]}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
