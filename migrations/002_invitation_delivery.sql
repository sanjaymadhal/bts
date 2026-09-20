-- Trackr schema migration #2 — invitation delivery tracking.
-- Adds columns to `invitations` so the admin UI can show whether the
-- welcome email went out and offer a "Resend" button when it didn't.
-- Also stores the plaintext temp password so the admin can re-send
-- the same invite without resetting the recipient's password.
--
-- The `temp_password` column is NEVER returned to mobile clients on
-- `GET /invitations` — the backend route strips it. The backend
-- uses it server-side only, to populate the re-send email body.
-- Idempotent — safe to re-run.

alter table public.invitations
  add column if not exists email_delivered boolean not null default false;

alter table public.invitations
  add column if not exists email_last_error text;

alter table public.invitations
  add column if not exists email_via text;

alter table public.invitations
  add column if not exists email_sent_at timestamptz;

alter table public.invitations
  add column if not exists temp_password text;
