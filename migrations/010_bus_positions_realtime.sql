-- Trackr migration #10 — wire `bus_positions` into realtime.
--
-- `bus_positions` was created in 001 but the `alter publication`
-- statement was left as a comment. Supabase Realtime only broadcasts
-- change events for tables that are members of the `supabase_realtime`
-- publication, so mobile clients subscribed via `postgres_changes`
-- never received a single frame — the live map froze at the last
-- cold-start seed and flipped to "offline" after the stale window.
--
-- This migration (idempotent, safe to re-run) does two things:
--   1. Adds `bus_positions` to the realtime publication (guarded like
--      006's notification tables).
--   2. Enables RLS + a school-scoped SELECT policy so an authenticated
--      user's realtime reads can actually see rows. Real-time reads
--      go through PostgREST with the user's JWT, so RLS applies; with
--      no policy the channel subscribes but every row is filtered out.

alter table public.bus_positions enable row level security;

-- Authenticated users (parents, admins) may read positions for buses
-- that belong to the tenant school. Parents are further scoped to
-- their own child's bus by the backend API (`/positions`); this policy
-- is the coarse realtime gate.
drop policy if exists bus_positions_select_authenticated on public.bus_positions;
create policy bus_positions_select_authenticated
on public.bus_positions
for select
to authenticated
using (
  exists (
    select 1 from public.buses b
    where b.id = bus_positions.bus_id
      and b.school_id = '00000000-0000-0000-0000-000000000001'::uuid
  )
);

do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'supabase_realtime'
      and schemaname = 'public'
      and tablename = 'bus_positions'
  ) then
    alter publication supabase_realtime add table public.bus_positions;
  end if;
end $$;