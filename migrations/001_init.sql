-- Trackr schema — single-tenant MVP for DPS East.
-- Seed the school row first; everything else defaults to it.

create extension if not exists "pgcrypto";

-- 1. Schools ----------------------------------------------------------------

create table if not exists public.schools (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  code        text not null unique,
  created_at  timestamptz default now()
);

insert into public.schools (id, name, code)
values (
  '00000000-0000-0000-0000-000000000001'::uuid,
  'Delhi Public School — East',
  'DPS-EAST'
)
on conflict (code) do nothing;

-- 2. Enums ------------------------------------------------------------------

do $$ begin
  create type user_role as enum (
    'admin', 'driver', 'staff', 'parent', 'student'
  );
exception when duplicate_object then null; end $$;

-- 3. Profiles ---------------------------------------------------------------

create table if not exists public.profiles (
  id               uuid primary key references auth.users(id) on delete cascade,
  role             user_role not null,
  school_id        uuid not null default '00000000-0000-0000-0000-000000000001'::uuid
                   references public.schools(id) on delete restrict,
  display_name     text not null,
  phone            text,
  linked_student_id uuid references public.students(id) on delete set null,
  assigned_bus_id   uuid references public.buses(id) on delete set null,
  created_at       timestamptz default now()
);

-- 4. Buses ------------------------------------------------------------------

create table if not exists public.buses (
  id          uuid primary key default gen_random_uuid(),
  school_id   uuid not null default '00000000-0000-0000-0000-000000000001'::uuid
               references public.schools(id) on delete cascade,
  number      text not null,
  route       text not null,
  capacity    int  not null,
  driver_id   uuid references public.profiles(id) on delete set null,
  schedule    jsonb not null default '[]',
  created_at  timestamptz default now()
);

-- 5. Students ---------------------------------------------------------------

create table if not exists public.students (
  id           uuid primary key default gen_random_uuid(),
  school_id    uuid not null default '00000000-0000-0000-0000-000000000001'::uuid
                references public.schools(id) on delete cascade,
  bus_id       uuid references public.buses(id) on delete set null,
  name         text not null,
  class_grade  text not null,
  stop         text not null,
  created_at   timestamptz default now()
);

-- 6. Bus positions ----------------------------------------------------------

create table if not exists public.bus_positions (
  bus_id      uuid primary key references public.buses(id) on delete cascade,
  latitude    double precision not null,
  longitude   double precision not null,
  speed       double precision not null default 0,
  altitude    double precision not null default 0,
  updated_at  timestamptz default now()
);

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

-- Realtime: only bus_positions is published.
-- Run after tables exist:
--   alter publication supabase_realtime add table public.bus_positions;

-- 7. Invitations ------------------------------------------------------------

create table if not exists public.invitations (
  id               uuid primary key default gen_random_uuid(),
  school_id        uuid not null default '00000000-0000-0000-0000-000000000001'::uuid
                    references public.schools(id) on delete cascade,
  email            text not null,
  role             user_role not null,
  accepted_user_id uuid references auth.users(id) on delete set null,
  display_name     text not null,
  temp_password_hash text not null,
  expires_at       timestamptz not null,
  created_at       timestamptz default now()
);

-- 8. School settings (single row, school-scoped) ----------------------------

create table if not exists public.school_settings (
  school_id               uuid primary key references public.schools(id) on delete cascade,
  notifications_enabled   boolean not null default true,
  auto_assign_stops       boolean not null default false,
  language                text not null default 'en',
  default_alert_radius_m  int not null default 250
);

insert into public.school_settings (school_id)
values ('00000000-0000-0000-0000-000000000001'::uuid)
on conflict (school_id) do nothing;

-- 9. Seed first admin -------------------------------------------------------
-- Run the SQL below separately (auth.users is Supabase-internal):
--
-- insert into auth.users (
--   id, instance_id, aud, role, email,
--   encrypted_password, email_confirmed_at,
--   raw_app_meta_data, raw_user_meta_data,
--   created_at, updated_at, confirmation_token,
--   email_change, email_change_token_new, recovery_token
-- ) values (
--   'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'::uuid,
--   '00000000-0000-0000-0000-000000000000'::uuid,
--   'authenticated', 'authenticated',
--   'admin@dps-east.test',
--   crypt('change-me-now', gen_salt('bf')),
--   now(),
--   '{"provider":"email","providers":["email"]}'::jsonb,
--   '{}'::jsonb,
--   now(), now(), '', '', '', ''
-- );
--
-- insert into public.profiles (id, role, display_name)
-- values (
--   'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'::uuid,
--   'admin',
--   'First Admin'
-- );
