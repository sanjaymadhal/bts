-- Trackr migration #6 — notification inbox + push tokens.
--
-- `notifications` is the per-user feed the inbox screen renders. The
-- simulator writes rows here as it walks the bus schedule; admins get
-- one row per stop transition, parents get one per "two stops away"
-- (or "arriving") event. Both rows are written by the same backend
-- fan-out so the inbox is the source of truth — the OS push is a
-- side-effect, not the primary record.
--
-- `push_tokens` stores the Expo push token per user. The frontend
-- POSTs to /notifications/token after the OS prompt; the backend
-- upserts and the simulator reads it when it dispatches.
--
-- Both tables are published to `supabase_realtime` so the frontend
-- `NotificationsProvider` can subscribe per-user (filtered by
-- `user_id` on the channel) and update the inbox live.
--
-- Idempotent — safe to re-run. The `alter publication` calls are
-- guarded so running the script twice doesn't fail.

create table if not exists public.push_tokens (
  user_id    uuid primary key references auth.users(id) on delete cascade,
  token      text not null,
  platform   text not null check (platform in ('ios','android','web')),
  updated_at timestamptz not null default now()
);

create table if not exists public.notifications (
  id          uuid primary key default gen_random_uuid(),
  school_id   uuid not null default '00000000-0000-0000-0000-000000000001'::uuid
              references public.schools(id) on delete cascade,
  user_id     uuid not null references auth.users(id) on delete cascade,
  kind        text not null check (kind in (
                'bus_at_stop',         -- admin: which stop is the bus at
                'bus_two_stops_away',  -- parent: K stops away
                'bus_arriving',        -- parent: <250m to pickup stop
                'bus_boarded',         -- parent: child boarded
                'bus_delayed'          -- admin: bus hasn't moved
              )),
  bus_id      uuid references public.buses(id) on delete cascade,
  student_id  uuid references public.students(id) on delete set null,
  title       text not null,
  body        text not null,
  data        jsonb not null default '{}'::jsonb,
  read_at     timestamptz,
  created_at  timestamptz not null default now()
);

create index if not exists notifications_user_idx
  on public.notifications(user_id, created_at desc);

-- Partial index keeps the unread-count query O(unread) instead of
-- O(rows). The predicate is the same one the inbox uses.
create index if not exists notifications_unread_idx
  on public.notifications(user_id) where read_at is null;

-- Publish to the realtime channel. The Supabase JS client filters
-- by `user_id` on the client side; the backend writes unconditionally
-- here so the inbox is always coherent across devices.
do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'supabase_realtime' and tablename = 'notifications'
  ) then
    alter publication supabase_realtime add table public.notifications;
  end if;
  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'supabase_realtime' and tablename = 'push_tokens'
  ) then
    alter publication supabase_realtime add table public.push_tokens;
  end if;
end $$;
