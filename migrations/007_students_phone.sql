-- Trackr migration #7 — students contact + stop identity.
--
-- `students.phone` is the parent / guardian contact for the child.
-- Used by the welcome email and by the notification fan-out as a
-- fallback target when push isn't available. Drivers already have
-- `profiles.phone`; students get their own column so we don't tie
-- driver contact to the child row.
--
-- `students.stop_id` is currently a free-text identifier that the
-- notification engine matches against `buses.schedule[*].name`. It
-- lives in its own column (rather than overloading `stop`) so the
-- matching layer can be promoted to a real FK in a follow-up
-- without a data migration. The text `stop` field is the human-
-- readable label the UI keeps showing.
--
-- Idempotent.

alter table public.students
  add column if not exists phone text;

alter table public.students
  add column if not exists stop_id text;
