-- GridOS SaaS — workbook collaborators (shared workbooks, phase 7).
-- Run in Supabase SQL Editor. Idempotent.
--
-- Lets a workbook owner grant 'editor' or 'viewer' access to other users by
-- email. The server resolves a collaborator's request to the *owner's* kernel
-- instance so both users see the same live cell state, and every write goes
-- through the same threadsafe commit path.
--
-- RLS on public.workbooks is extended so a collaborator can SELECT and (if
-- editor) UPDATE the workbook row. The server still uses the service-role
-- key for the hot read/write path, but RLS stays as the defense-in-depth
-- layer in case a client key ever leaks.

-- ========== Collaborators table ==========
create table if not exists public.workbook_collaborators (
    workbook_id uuid not null references public.workbooks(id) on delete cascade,
    user_id     uuid not null references public.users(id)     on delete cascade,
    role        text not null default 'editor'
                check (role in ('editor', 'viewer')),
    -- Who issued the invite. Kept for audit + "invited by Alice" UI; cascades
    -- to null rather than delete so a deleted inviter doesn't wipe the grant.
    invited_by  uuid references public.users(id) on delete set null,
    invited_at  timestamptz not null default now(),
    accepted_at timestamptz,
    primary key (workbook_id, user_id)
);

-- Lookup path "which workbooks has this user been invited into" — this is
-- the query for the "Shared with me" list.
create index if not exists workbook_collaborators_user_idx
    on public.workbook_collaborators(user_id);

-- ========== RLS ==========
alter table public.workbook_collaborators enable row level security;

-- A collaborator row is visible to:
--   a) the collaborator themselves (so "Shared with me" works client-side)
--   b) the workbook owner (so the Share… modal can list grants)
drop policy if exists collabs_select_self_or_owner on public.workbook_collaborators;
create policy collabs_select_self_or_owner on public.workbook_collaborators
    for select using (
        auth.uid() = user_id
        or auth.uid() in (
            select w.user_id from public.workbooks w where w.id = workbook_id
        )
    );

-- Only the workbook owner can insert/update/delete grants. Collaborators
-- can't re-share or self-demote via the client.
drop policy if exists collabs_owner_insert on public.workbook_collaborators;
create policy collabs_owner_insert on public.workbook_collaborators
    for insert with check (
        auth.uid() in (
            select w.user_id from public.workbooks w where w.id = workbook_id
        )
    );

drop policy if exists collabs_owner_update on public.workbook_collaborators;
create policy collabs_owner_update on public.workbook_collaborators
    for update using (
        auth.uid() in (
            select w.user_id from public.workbooks w where w.id = workbook_id
        )
    );

drop policy if exists collabs_owner_delete on public.workbook_collaborators;
create policy collabs_owner_delete on public.workbook_collaborators
    for delete using (
        auth.uid() in (
            select w.user_id from public.workbooks w where w.id = workbook_id
        )
    );

-- ========== Extend public.workbooks RLS for collaborators ==========
-- A collaborator can SELECT the workbook row; editors can also UPDATE.
-- We DROP + recreate the existing own-only policies so collaborators are
-- folded in. (Inserts and deletes stay owner-only — a collaborator can't
-- create or delete your workbook.)

drop policy if exists workbooks_select_own on public.workbooks;
create policy workbooks_select_own on public.workbooks
    for select using (
        auth.uid() = user_id
        or auth.uid() in (
            select c.user_id from public.workbook_collaborators c
            where c.workbook_id = public.workbooks.id
        )
    );

drop policy if exists workbooks_update_own on public.workbooks;
create policy workbooks_update_own on public.workbooks
    for update using (
        auth.uid() = user_id
        or auth.uid() in (
            select c.user_id from public.workbook_collaborators c
            where c.workbook_id = public.workbooks.id
              and c.role = 'editor'
        )
    ) with check (
        auth.uid() = user_id
        or auth.uid() in (
            select c.user_id from public.workbook_collaborators c
            where c.workbook_id = public.workbooks.id
              and c.role = 'editor'
        )
    );

-- insert/delete policies stay owner-only (unchanged from 0001_init.sql).
