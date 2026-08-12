-- Trackr migration: mark invite-created profiles as requiring a password change on first login.

alter table if exists public.profiles
  add column if not exists must_change_password boolean not null default false;

update public.profiles
set must_change_password = coalesce(must_change_password, false)
where must_change_password is null;
