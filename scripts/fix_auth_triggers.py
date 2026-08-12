#!/usr/bin/env python
"""Disable custom triggers on `auth.users` so admin.create_user stops 500-ing.

Background
----------
The hosted Supabase GoTrue service inserts into `auth.users` and runs
any `AFTER INSERT` triggers defined on it. A common cause of the
generic `Database error creating new user` 500 is a custom trigger
whose function has been deleted (e.g. someone tried to create a
`handle_new_user` trigger to auto-populate `public.profiles`, and the
function was dropped later). Every new signup then 500s.

This script connects to the project's Postgres directly using the
connection string you paste in, lists custom triggers on `auth.users`,
and disables each one. You can re-enable specific triggers with
`ALTER TABLE auth.users ENABLE TRIGGER <name>` once you've fixed the
underlying function.

Run from trackr-backend/ with the venv active:

    .venv/Scripts/python.exe scripts/fix_auth_triggers.py
"""
from __future__ import annotations

import getpass
import sys
from urllib.parse import quote

# `psycopg2` is the standard Postgres driver; install if missing.
try:
    import psycopg2  # type: ignore
    from psycopg2 import sql  # type: ignore
except ImportError:
    print(
        "This script needs psycopg2. Install it with:\n"
        "  .venv/Scripts/pip.exe install psycopg2-binary"
    )
    sys.exit(1)


def main() -> int:
    print("Paste your Supabase Postgres connection string.")
    print(
        "Find it in Supabase → Project Settings → Database → "
        "Connection string → URI. It looks like:\n"
        "  postgresql://postgres:<password>@db.<ref>.supabase.co:5432/postgres"
    )
    conn_str = getpass.getpass("Connection URI (input is hidden): ").strip()
    if not conn_str.startswith("postgresql://") and not conn_str.startswith("postgres://"):
        print("Connection string must start with postgresql:// or postgres://")
        return 1

    print("\nConnecting…")
    try:
        conn = psycopg2.connect(conn_str)
    except Exception as exc:
        print(f"Failed to connect: {exc}")
        return 1
    conn.autocommit = True
    cur = conn.cursor()

    print("\nCustom triggers on auth.users (system triggers excluded):")
    cur.execute(
        """
        SELECT t.tgname, p.proname, t.tgenabled
        FROM pg_trigger t
        JOIN pg_proc p ON p.oid = t.tgfoid
        WHERE t.tgrelid = 'auth.users'::regclass
          AND NOT t.tgisinternal
        ORDER BY t.tgname
        """
    )
    triggers = cur.fetchall()
    if not triggers:
        print("  (none) — nothing to disable. The 500 is from somewhere else.")
        return 0
    for name, func, enabled in triggers:
        print(f"  {name}  function={func}  enabled={enabled}")

    answer = input("\nDisable these triggers? [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted.")
        return 0

    for name, _func, _enabled in triggers:
        cur.execute(
            sql.SQL("ALTER TABLE auth.users DISABLE TRIGGER {}").format(
                sql.Identifier(name)
            )
        )
        print(f"  disabled: {name}")

    print("\nDone. Try the admin app's invite flow again.")
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
