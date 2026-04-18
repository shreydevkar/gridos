-- GridOS SaaS schema — Phase 2.
-- Run in Supabase SQL Editor (dashboard → SQL). Idempotent where possible so
-- you can re-run while iterating. Assumes the default `auth.users` table from
-- Supabase Auth is in place; don't disable that extension.
--
-- Rough flow once this is live:
--   1. Supabase Auth creates a row in auth.users on signup.
--   2. A trigger (below) mirrors that into public.users with tier=free.
--   3. Every request is authenticated via the Supabase JWT → auth.uid() → RLS
--      ensures users only see their own workbooks / usage_logs / user_usage row.
--
-- Re-apply safely: this file uses IF NOT EXISTS and DROP POLICY IF EXISTS so
-- you can paste it repeatedly without breaking anything mid-migration.

-- ========== Extensions ==========
create extension if not exists "pgcrypto";   -- gen_random_uuid()

-- ========== Tables ==========

create table if not exists public.users (
    id uuid primary key references auth.users(id) on delete cascade,
    email text unique not null,
    subscription_tier text not null default 'free'
        check (subscription_tier in ('free', 'pro', 'enterprise')),
    stripe_customer_id text,
    created_at timestamptz not null default now()
);

create table if not exists public.workbooks (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.users(id) on delete cascade,
    title text not null default 'Untitled workbook',
    -- Full export_state_dict() payload. chat_log is already embedded by the
    -- kernel so no separate column is needed today; if we ever want to query
    -- chat turns cross-workbook we'll promote it to its own table.
    grid_state jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists workbooks_user_id_idx on public.workbooks(user_id);
create index if not exists workbooks_updated_at_idx on public.workbooks(updated_at desc);

-- Per-call telemetry. Keep lean (no raw prompts) so we can keep it forever
-- without PII review — the aggregates table below is what quota enforcement
-- actually reads.
create table if not exists public.usage_logs (
    id bigserial primary key,
    user_id uuid not null references public.users(id) on delete cascade,
    provider text not null,
    model text not null,
    prompt_tokens int not null default 0,
    completion_tokens int not null default 0,
    finish_reason text,
    workbook_id uuid references public.workbooks(id) on delete set null,
    created_at timestamptz not null default now()
);

create index if not exists usage_logs_user_created_idx
    on public.usage_logs(user_id, created_at desc);

-- Monthly rollup for O(1) quota checks. The server upserts into this table on
-- every successful LLM call; /agent/chat reads it to enforce tier limits.
create table if not exists public.user_usage (
    user_id uuid not null references public.users(id) on delete cascade,
    month date not null,
    total_tokens bigint not null default 0,
    cost_cents bigint not null default 0,
    primary key (user_id, month)
);

-- ========== updated_at trigger for workbooks ==========
create or replace function public.touch_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

drop trigger if exists workbooks_touch_updated_at on public.workbooks;
create trigger workbooks_touch_updated_at
    before update on public.workbooks
    for each row execute function public.touch_updated_at();

-- ========== Signup mirror trigger ==========
-- When Supabase Auth inserts into auth.users, mirror the row into public.users.
create or replace function public.handle_new_auth_user()
returns trigger language plpgsql security definer as $$
begin
    insert into public.users (id, email)
    values (new.id, new.email)
    on conflict (id) do nothing;
    return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_auth_user();

-- ========== Row Level Security ==========
alter table public.users         enable row level security;
alter table public.workbooks     enable row level security;
alter table public.usage_logs    enable row level security;
alter table public.user_usage    enable row level security;

-- users: you can only see / update your own row.
drop policy if exists users_select_own on public.users;
create policy users_select_own on public.users
    for select using (auth.uid() = id);

drop policy if exists users_update_own on public.users;
create policy users_update_own on public.users
    for update using (auth.uid() = id);

-- workbooks: full CRUD on rows you own.
drop policy if exists workbooks_select_own on public.workbooks;
create policy workbooks_select_own on public.workbooks
    for select using (auth.uid() = user_id);

drop policy if exists workbooks_insert_own on public.workbooks;
create policy workbooks_insert_own on public.workbooks
    for insert with check (auth.uid() = user_id);

drop policy if exists workbooks_update_own on public.workbooks;
create policy workbooks_update_own on public.workbooks
    for update using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists workbooks_delete_own on public.workbooks;
create policy workbooks_delete_own on public.workbooks
    for delete using (auth.uid() = user_id);

-- usage_logs: read-only for the end user; server writes via the service-role
-- key which bypasses RLS. No INSERT/UPDATE policy on purpose.
drop policy if exists usage_logs_select_own on public.usage_logs;
create policy usage_logs_select_own on public.usage_logs
    for select using (auth.uid() = user_id);

-- user_usage: same deal — read-only for end users, server writes via service key.
drop policy if exists user_usage_select_own on public.user_usage;
create policy user_usage_select_own on public.user_usage
    for select using (auth.uid() = user_id);
