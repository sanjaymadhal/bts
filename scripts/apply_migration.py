#!/usr/bin/env python
"""Apply a SQL migration to the Supabase project via PostgREST RPC.

If your project doesn't expose an `exec_sql` RPC, this script falls
back to printing the SQL for you to paste into the Supabase SQL
Editor.

Run from trackr-backend/ with the venv active:

    .venv/Scripts/python.exe scripts/apply_migration.py migrations/002_invitation_delivery.sql
"""
from __future__ import annotations

import getpass
import sys
import urllib.error
import urllib.request


def main(path: str) -> int:
    sql = open(path, "r", encoding="utf-8").read()
    print(f"=== Migration: {path} ({len(sql)} chars) ===\n")

    print("Paste the SERVICE ROLE key (Supabase → Project Settings → API →")
    print("`service_role` / `secret` key).\n")
    service_role = getpass.getpass("Service role key: ").strip()
    project_url = input("Project URL (e.g. https://rzcbmyedznuzwuvybdue.supabase.co): ").strip()
    project_url = project_url.rstrip("/")
    if not project_url.startswith("https://"):
        print("Invalid URL.")
        return 1

    print("\nTrying PostgREST RPC `exec_sql`…")
    req = urllib.request.Request(
        f"{project_url}/rest/v1/rpc/exec_sql",
        method="POST",
        headers={
            "apikey": service_role,
            "Authorization": f"Bearer {service_role}",
            "Content-Type": "application/json",
        },
        data=sql.encode("utf-8"),
    )
    try:
        with urllib.request.urlopen(req) as resp:
            print(f"RPC OK ({resp.status}).")
            return 0
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        if e.code == 404:
            print("\nNo `exec_sql` RPC exposed in your project.")
            print("Run this SQL in Supabase → SQL Editor → New query → Paste → Run:\n")
            print("=" * 70)
            print(sql)
            print("=" * 70)
            return 0
        print(f"RPC failed ({e.code}): {body[:400]}")
        return 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: apply_migration.py <path-to-sql>")
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
