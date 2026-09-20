-- Add live position metrics to databases created before 001_init.sql included
-- them, and keep updated_at accurate for realtime consumers and health checks.

alter table public.bus_positions
  add column if not exists speed double precision not null default 0,
  add column if not exists altitude double precision not null default 0;

create or replace function public.set_bus_position_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists bus_positions_set_updated_at on public.bus_positions;
create trigger bus_positions_set_updated_at
before update on public.bus_positions
for each row execute function public.set_bus_position_updated_at();

notify pgrst, 'reload schema';