-- Phase 6 — per-user plugin marketplace installs.
--
-- Plugins themselves are shipped in the `plugins/` directory of the repo and
-- loaded globally at server boot — see core/plugins.py. This table tracks
-- WHICH of those shipped plugins each user has chosen to install into their
-- working system, so the in-app Marketplace UI can surface the right toggle
-- state across devices.
--
-- RLS: users only see and mutate their own rows. The server uses the
-- service-role client (bypasses RLS), so policies are defense-in-depth.

create table if not exists public.user_plugins (
    user_id      uuid        not null references auth.users(id) on delete cascade,
    plugin_slug  text        not null,
    enabled      boolean     not null default true,
    installed_at timestamptz not null default now(),
    primary key (user_id, plugin_slug)
);

alter table public.user_plugins enable row level security;

drop policy if exists "users read own plugins"   on public.user_plugins;
drop policy if exists "users insert own plugins" on public.user_plugins;
drop policy if exists "users update own plugins" on public.user_plugins;
drop policy if exists "users delete own plugins" on public.user_plugins;

create policy "users read own plugins"   on public.user_plugins for select using (auth.uid() = user_id);
create policy "users insert own plugins" on public.user_plugins for insert with check (auth.uid() = user_id);
create policy "users update own plugins" on public.user_plugins for update using (auth.uid() = user_id);
create policy "users delete own plugins" on public.user_plugins for delete using (auth.uid() = user_id);
