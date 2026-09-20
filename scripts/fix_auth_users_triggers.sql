-- Fix for: Supabase `auth.admin.create_user` returns
--   HTTP 500 "Database error creating new user"
--
-- Background
-- ----------
-- The hosted GoTrue service writes to `auth.users` and fires any
-- `AFTER INSERT` triggers defined on it. A common cause of this 500
-- is a custom `on_auth_user_created` trigger whose function has been
-- deleted, OR a `BEFORE INSERT` trigger that's failing for every row.
--
-- The cleanest diagnostic is to inspect and disable any custom
-- triggers on `auth.users`, then retry the invite flow.
--
-- Run this in Supabase → SQL Editor.
--
-- ⚠️  This script only DROPS custom triggers on auth.users. It does
--     NOT touch your `public.profiles` table or any user rows.

-- 1. List existing triggers on auth.users so we know what we're about
--    to remove.
SELECT
  t.tgname AS trigger_name,
  p.proname AS function_name,
  t.tgenabled AS enabled
FROM pg_trigger t
JOIN pg_proc p ON p.oid = t.tgfoid
WHERE t.tgrelid = 'auth.users'::regclass
  AND NOT t.tgisinternal;       -- exclude system triggers

-- 2. Disable every custom (non-internal) trigger on auth.users so the
--    GoTrue service can insert without interruption. If a trigger is
--    essential (e.g. it auto-populates a related table), re-enable it
--    afterwards and fix the function — don't leave it disabled.
DO $$
DECLARE
  trg RECORD;
BEGIN
  FOR trg IN
    SELECT t.tgname
    FROM pg_trigger t
    WHERE t.tgrelid = 'auth.users'::regclass
      AND NOT t.tgisinternal
  LOOP
    EXECUTE format('ALTER TABLE auth.users DISABLE TRIGGER %I', trg.tgname);
    RAISE NOTICE 'Disabled trigger: %', trg.tgname;
  END LOOP;
END $$;

-- 3. Verify: the next invite call from the admin app should succeed.
--    Re-enable triggers later with:
--      ALTER TABLE auth.users ENABLE TRIGGER <name>;
