-- GridOS SaaS — fix pending_invites uniqueness for ON CONFLICT upserts.
-- Run in Supabase SQL Editor. Idempotent.
--
-- The initial 0008 migration used a partial expression unique INDEX:
--   create unique index ... (workbook_id, lower(email)) where accepted_at is null;
-- PostgREST upserts target the constraint name via ON CONFLICT, and Postgres
-- can only match ON CONFLICT against a plain unique CONSTRAINT (not a partial
-- or expression index). Invites to unregistered users therefore failed with
--   42P10: there is no unique or exclusion constraint matching the ON CONFLICT...
--
-- This migration swaps the partial index for a plain unique constraint on
-- (workbook_id, email). Emails are lowercased at the insert site in Python,
-- so we don't need lower() in the constraint to enforce case-insensitivity.

drop index if exists public.pending_invites_workbook_email_idx;

alter table public.pending_invites
    drop constraint if exists pending_invites_workbook_email_key;
alter table public.pending_invites
    add constraint pending_invites_workbook_email_key
    unique (workbook_id, email);
