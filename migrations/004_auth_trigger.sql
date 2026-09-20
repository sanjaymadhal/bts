-- Trackr migration: Add trigger to automatically create profiles when Supabase creates auth.users.
-- This is the canonical source for profile creation; the backend should never manually create profiles.

-- Create the trigger function that fires when a new auth user is created.
create or replace function public.handle_new_auth_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.profiles (
    id,
    role,
    school_id,
    display_name,
    phone,
    linked_student_id,
    assigned_bus_id,
    must_change_password
  )
  values (
    new.id,
    coalesce(
      (new.raw_user_meta_data ->> 'role')::public.user_role,
      'parent'::public.user_role
    ),
    '00000000-0000-0000-0000-000000000001'::uuid,
    coalesce(
      new.raw_user_meta_data ->> 'display_name',
      split_part(new.email, '@', 1)
    ),
    new.raw_user_meta_data ->> 'phone',
    (new.raw_user_meta_data ->> 'linked_student_id')::uuid,
    (new.raw_user_meta_data ->> 'assigned_bus_id')::uuid,
    coalesce(
      (new.raw_user_meta_data ->> 'must_change_password')::boolean,
      false
    )
  );
  return new;
exception when unique_violation then
  -- Profile already exists. This can happen on re-invitation.
  -- Update it instead of failing.
  update public.profiles
  set
    role = coalesce(
      (new.raw_user_meta_data ->> 'role')::public.user_role,
      role
    ),
    display_name = coalesce(
      new.raw_user_meta_data ->> 'display_name',
      display_name
    ),
    phone = coalesce(
      new.raw_user_meta_data ->> 'phone',
      phone
    ),
    linked_student_id = coalesce(
      (new.raw_user_meta_data ->> 'linked_student_id')::uuid,
      linked_student_id
    ),
    assigned_bus_id = coalesce(
      (new.raw_user_meta_data ->> 'assigned_bus_id')::uuid,
      assigned_bus_id
    ),
    must_change_password = coalesce(
      (new.raw_user_meta_data ->> 'must_change_password')::boolean,
      must_change_password
    )
  where id = new.id;
  return new;
end;
$$;

-- Drop existing trigger if it exists and recreate it.
drop trigger if exists on_auth_user_created on auth.users;

-- Create the trigger that fires after each new auth user is inserted.
create trigger on_auth_user_created
  after insert on auth.users
  for each row
  execute function public.handle_new_auth_user();
