-- GridOS SaaS — per-user plugin secrets (phase 7.2).
-- Run in Supabase SQL Editor. Idempotent.
--
-- Parallel to user_api_keys (BYOK for LLM providers), this table stores
-- per-user secrets that plugins need to hit third-party APIs on behalf of
-- the signed-in user. Shopify store domain + admin token, Stripe secret
-- key, optional GITHUB_TOKEN for higher rate limits, etc. Each plugin
-- declares the set of secrets it needs in its manifest.json; the
-- marketplace UI renders a form from that declaration and posts back
-- to /settings/plugin-secrets/{slug}.
--
-- Server reads secrets via cloud.user_plugin_secrets.get_all_for(user_id)
-- and stuffs them into a per-request ContextVar. Plugins resolve them via
-- kernel.get_secret(slug, key_name) which returns the per-user value in
-- SaaS OR falls back to the matching OS env var in OSS / when no row is
-- set, so local dev + operator-scoped deployments keep working.

create table if not exists public.user_plugin_secrets (
    user_id       uuid not null references public.users(id) on delete cascade,
    plugin_slug   text not null,
    secret_key    text not null,
    -- Stored as-is. Per-row RLS + the service-role-only server key gate
    -- keep it out of reach of anyone except the owning user and the
    -- server. If plaintext-in-DB ever becomes a compliance blocker we can
    -- layer pgsodium or app-level KMS on top without changing the schema
    -- shape.
    secret_value  text not null,
    updated_at    timestamptz not null default now(),
    primary key (user_id, plugin_slug, secret_key)
);

create index if not exists user_plugin_secrets_user_idx
    on public.user_plugin_secrets(user_id);

-- Refresh updated_at on update so the Settings UI can show "last updated".
drop trigger if exists user_plugin_secrets_touch on public.user_plugin_secrets;
create trigger user_plugin_secrets_touch
    before update on public.user_plugin_secrets
    for each row execute function public.touch_updated_at();

-- RLS — you can only see, set, or delete your own rows. The server uses
-- the service-role key for the hot path, so this is the defense layer
-- in case a client key ever leaks.
alter table public.user_plugin_secrets enable row level security;

drop policy if exists plugin_secrets_select_own on public.user_plugin_secrets;
create policy plugin_secrets_select_own on public.user_plugin_secrets
    for select using (auth.uid() = user_id);

drop policy if exists plugin_secrets_insert_own on public.user_plugin_secrets;
create policy plugin_secrets_insert_own on public.user_plugin_secrets
    for insert with check (auth.uid() = user_id);

drop policy if exists plugin_secrets_update_own on public.user_plugin_secrets;
create policy plugin_secrets_update_own on public.user_plugin_secrets
    for update using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists plugin_secrets_delete_own on public.user_plugin_secrets;
create policy plugin_secrets_delete_own on public.user_plugin_secrets
    for delete using (auth.uid() = user_id);
