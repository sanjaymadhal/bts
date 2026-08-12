-- Trackr migration #5 — per-user notification preferences.
--
-- Mirrors the toggle list in the parent settings screen
-- (arriving / twostop / fivemin / boarded / delayed). The UI today
-- reads from local AsyncStorage; this table is the home of the
-- eventual cloud sync. The defaults match the in-app defaults so
-- a fresh user gets the same experience whether the row exists
-- or not.
--
-- Idempotent — safe to re-run.

create table if not exists public.notification_preferences (
  user_id        uuid primary key references auth.users(id) on delete cascade,
  push_enabled   boolean not null default true,
  arriving       boolean not null default true,
  twostop        boolean not null default true,
  fivemin        boolean not null default true,
  boarded        boolean not null default true,
  delayed        boolean not null default true,
  updated_at     timestamptz not null default now()
);
