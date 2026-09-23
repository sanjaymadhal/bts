-- Trackr migration #11 — per-day history counts.
--
-- The 30-day trend chart used to depend on the last 500 raw points
-- (≈25 minutes at the 3s MQTT cadence), so every bucket but the most
-- recent was zero. This SQL function aggregates counts per calendar
-- day for the whole requested window, letting the frontend render a
-- truthful chart with one cheap query instead of a huge row dump.
--
-- The service-role backend calls it as
--   supabase.rpc("bus_position_daily_counts", { bus_id, since })

create or replace function public.bus_position_daily_counts(
  bus_id uuid,
  since timestamptz
)
returns table (day date, count bigint)
language sql
stable
as $$
  select created_at::date as day, count(*)::bigint as count
  from public.bus_position_history
  where public.bus_position_history.bus_id = bus_position_daily_counts.bus_id
    and created_at >= since
  group by created_at::date
  order by day asc;
$$;