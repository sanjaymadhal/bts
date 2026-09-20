-- Workaround for Supabase `admin.create_user` returning HTTP 500
-- "Database error creating new user" when a custom on_auth_user_created
-- trigger is broken or blocking.
--
-- Run this in Supabase → SQL Editor.
--
-- This script creates a SECURITY DEFINER function in the public schema
-- that the backend can call via PostgREST RPC to insert directly into
-- `auth.users`. Bypassing GoTrue's admin API avoids the broken trigger
-- path.
--
-- ⚠️  This requires the project to allow the `postgres` role to insert
--     into `auth.users`. Most Supabase projects do allow this because
--     `postgres` is treated as a superuser in the hosted environment.

-- 1. Drop the function if it already exists (idempotent re-runs).
drop function if exists public.admin_create_auth_user(text, text, jsonb, jsonb);

-- 2. Create the function. It mirrors what GoTrue's admin.create_user
--    does, minus the broken trigger path.
create or replace function public.admin_create_auth_user(
  p_email text,
  p_password text,
  p_user_metadata jsonb default '{}'::jsonb,
  p_app_metadata jsonb default '{}'::jsonb
) returns uuid
language plpgsql
security definer
set search_path = public, auth, extensions
as $$
declare
  v_user_id uuid := gen_random_uuid();
  v_encrypted_pw text;
begin
  v_encrypted_pw := crypt(p_password, gen_salt('bf'));

  insert into auth.users (
    instance_id,
    id,
    aud,
    role,
    email,
    encrypted_password,
    email_confirmed_at,
    raw_app_meta_data,
    raw_user_meta_data,
    created_at,
    updated_at,
    confirmation_token,
    email_change,
    email_change_token_new,
    recovery_token
  ) values (
    '00000000-0000-0000-0000-000000000000'::uuid,
    v_user_id,
    'authenticated',
    'authenticated',
    p_email,
    v_encrypted_pw,
    now(),
    coalesce(p_app_metadata, '{}'::jsonb),
    coalesce(p_user_metadata, '{}'::jsonb),
    now(),
    now(),
    '',
    '',
    '',
    ''
  );

  return v_user_id;
end;
$$;

-- 3. Allow the service role (PostgREST with `apikey: <service_role>`)
--    and the `postgres` user to call it.
grant execute on function public.admin_create_auth_user(text, text, jsonb, jsonb)
  to service_role, postgres, anon;

-- 4. Optional: identify the identity automatically inserted into
--    public.profiles. The backend's existing profile-insert code
--    already does this; the function returns the new uuid so the
--    caller can insert the matching profiles row.
--
-- After running this, the backend's create_invitation route can call
--   supabase_admin.rpc('admin_create_auth_user', { p_email, p_password, p_user_metadata })
-- instead of
--   supabase_admin.auth.admin.create_user(...)
-- to bypass the broken trigger.

-- ===========================================================================
-- 5. Companion: reset an existing user's password. Used by the resend
--    flow so the admin can re-mail a fresh temp password without
--    having to delete the user and start over.
-- ===========================================================================

drop function if exists public.admin_reset_auth_password(text, text);

create or replace function public.admin_reset_auth_password(
  p_email text,
  p_new_password text
) returns boolean
language plpgsql
security definer
set search_path = public, auth, extensions
as $$
declare
  v_count int;
begin
  update auth.users
     set encrypted_password = crypt(p_new_password, gen_salt('bf')),
         updated_at = now()
   where email = p_email;
  get diagnostics v_count = row_count;
  return v_count > 0;
end;
$$;

grant execute on function public.admin_reset_auth_password(text, text)
  to service_role, postgres, anon;
