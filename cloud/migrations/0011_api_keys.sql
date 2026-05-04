-- GridOS SaaS — per-user API keys for the Engine API (phase 8).
-- Run in Supabase SQL Editor. Idempotent.
--
-- API keys are how external AI agents authenticate to /eval, /schema, /peek
-- (and any other endpoint that uses require_user). The user mints a key from
-- the Settings UI; the server returns the full key ONCE on creation and
-- stores only its sha256 hash. Subsequent requests carry the key in
-- `Authorization: Bearer gridos_live_sk_<...>`; require_user matches on
-- the prefix and looks up the row by hash.
--
-- Soft-delete via revoked_at (not hard delete) so the audit trail survives
-- key rotation. Lookup query filters revoked_at IS NULL.

create table if not exists public.api_keys (
    id            uuid        primary key default gen_random_uuid(),
    user_id       uuid        not null references public.users(id) on delete cascade,
    name          text        not null,
    -- sha256(full_key) — never the plaintext. Lookups hash the incoming
    -- bearer token and match here. 64 hex chars.
    key_hash      text        not null unique,
    -- First 16 chars of the full key, e.g. "gridos_live_sk_a". Shown in the
    -- Settings UI so the user can identify which key is which without
    -- being able to recover the secret. Not load-bearing for auth.
    prefix        text        not null,
    created_at    timestamptz not null default now(),
    last_used_at  timestamptz,
    revoked_at    timestamptz
);

-- Hot path: lookup by key_hash on every authenticated request that uses an
-- API key. Index hits are sub-millisecond; without it auth scales linearly
-- in the table size.
create index if not exists api_keys_hash_idx on public.api_keys(key_hash) where revoked_at is null;

-- Listing path: SELECT ... WHERE user_id = ? ORDER BY created_at DESC.
create index if not exists api_keys_user_idx on public.api_keys(user_id, created_at desc);

-- RLS — users see and mutate only their own keys. Service-role server
-- bypasses RLS for the hot lookup path; this is defense in depth in case a
-- client-side anon key ever ends up with table access.
alter table public.api_keys enable row level security;

drop policy if exists api_keys_select_own on public.api_keys;
create policy api_keys_select_own on public.api_keys
    for select using (auth.uid() = user_id);

drop policy if exists api_keys_insert_own on public.api_keys;
create policy api_keys_insert_own on public.api_keys
    for insert with check (auth.uid() = user_id);

drop policy if exists api_keys_update_own on public.api_keys;
create policy api_keys_update_own on public.api_keys
    for update using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists api_keys_delete_own on public.api_keys;
create policy api_keys_delete_own on public.api_keys
    for delete using (auth.uid() = user_id);
