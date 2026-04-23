-- GridOS SaaS — pending invites for unregistered users (phase 7.1).
-- Run in Supabase SQL Editor. Idempotent.
--
-- Extends shared workbooks so the owner can invite by email BEFORE the
-- invitee has a GridOS account. Row lives in pending_invites until the
-- invitee signs up; an AFTER INSERT trigger on public.users promotes every
-- matching email to workbook_collaborators atomically, so the invitee sees
-- the shared workbook on their first visit.

-- ========== Pending invites table ==========
create table if not exists public.pending_invites (
    id uuid primary key default gen_random_uuid(),
    workbook_id uuid not null references public.workbooks(id) on delete cascade,
    email text not null,
    role text not null default 'editor'
          check (role in ('editor', 'viewer')),
    -- Inviter; cascade to null on account deletion so a revoked-owner row
    -- doesn't silently delete the invite. If the workbook itself is deleted
    -- the cascade above cleans this up.
    invited_by uuid references public.users(id) on delete set null,
    invited_at timestamptz not null default now(),
    accepted_at timestamptz
);

-- One pending invite per (workbook, email) at a time. Re-inviting the same
-- email upserts. Partial unique index so a promoted row (accepted_at set)
-- doesn't block a fresh invite if the owner later revokes + re-invites.
create unique index if not exists pending_invites_workbook_email_idx
    on public.pending_invites(workbook_id, lower(email))
    where accepted_at is null;

-- Fast lookup "which workbooks is <email> invited to" — drives the
-- promotion trigger on signup.
create index if not exists pending_invites_email_idx
    on public.pending_invites(lower(email))
    where accepted_at is null;

-- ========== RLS ==========
alter table public.pending_invites enable row level security;

-- A pending invite row is visible to the workbook owner (for managing
-- the Share… modal) and to the auth'd user whose email matches the
-- invite (so they can see pending invites for themselves in client UI
-- if we ever surface that — not used in v1).
drop policy if exists pending_select_owner_or_self on public.pending_invites;
create policy pending_select_owner_or_self on public.pending_invites
    for select using (
        auth.uid() in (select w.user_id from public.workbooks w where w.id = workbook_id)
        or lower(email) = lower((auth.jwt() ->> 'email'))
    );

-- Only the workbook owner can insert/delete pending invites.
drop policy if exists pending_owner_insert on public.pending_invites;
create policy pending_owner_insert on public.pending_invites
    for insert with check (
        auth.uid() in (select w.user_id from public.workbooks w where w.id = workbook_id)
    );

drop policy if exists pending_owner_delete on public.pending_invites;
create policy pending_owner_delete on public.pending_invites
    for delete using (
        auth.uid() in (select w.user_id from public.workbooks w where w.id = workbook_id)
    );

-- ========== Promotion trigger ==========
-- When a fresh row lands in public.users (mirrored from auth.users by the
-- existing handle_new_auth_user trigger), sweep pending_invites for the
-- same email and promote each row into workbook_collaborators. Single
-- atomic transaction with the user insert — if it fails, the signup fails
-- and the invitee gets a retry without a half-broken state.
create or replace function public.promote_pending_invites()
returns trigger language plpgsql security definer as $$
begin
    insert into public.workbook_collaborators
        (workbook_id, user_id, role, invited_by, invited_at, accepted_at)
    select
        pi.workbook_id,
        new.id,
        pi.role,
        pi.invited_by,
        pi.invited_at,
        now()
    from public.pending_invites pi
    where lower(pi.email) = lower(new.email)
      and pi.accepted_at is null
    on conflict (workbook_id, user_id) do update
        set role = excluded.role,
            invited_by = excluded.invited_by;

    update public.pending_invites
       set accepted_at = now()
     where lower(email) = lower(new.email)
       and accepted_at is null;

    return new;
end;
$$;

drop trigger if exists on_public_user_promote_invites on public.users;
create trigger on_public_user_promote_invites
    after insert on public.users
    for each row execute function public.promote_pending_invites();
