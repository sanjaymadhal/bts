-- 008_bus_history.sql
-- Track every single position update for history and analytics.

create table if not exists public.bus_position_history (
  id          uuid primary key default gen_random_uuid(),
  bus_id      uuid not null references public.buses(id) on delete cascade,
  latitude    double precision not null,
  longitude   double precision not null,
  speed       double precision default 0,
  altitude    double precision default 0,
  created_at  timestamptz default now()
);

-- Index for fast retrieval of a bus's history over time.
create index if not exists idx_bus_history_bus_id_time on public.bus_position_history (bus_id, created_at desc);
