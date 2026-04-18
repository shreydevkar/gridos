-- Phase 5b — per-user LLM API keys (BYOK).
--
-- The hosted SaaS tier charges for GridOS itself, not for LLM tokens. Each
-- user brings their own Gemini/Anthropic/Groq/OpenRouter key; the server
-- reads their row at request time and never sees another user's keys.
--
-- RLS is defense-in-depth only — the server uses the service-role client
-- which bypasses it — but if the service key ever leaked to a browser
-- build, a user still could not read another user's keys.

create table if not exists public.user_api_keys (
    user_id     uuid        not null references auth.users(id) on delete cascade,
    provider_id text        not null check (provider_id in ('gemini','anthropic','groq','openrouter')),
    api_key     text        not null,
    updated_at  timestamptz not null default now(),
    primary key (user_id, provider_id)
);

alter table public.user_api_keys enable row level security;

drop policy if exists "users read own keys"   on public.user_api_keys;
drop policy if exists "users insert own keys" on public.user_api_keys;
drop policy if exists "users update own keys" on public.user_api_keys;
drop policy if exists "users delete own keys" on public.user_api_keys;

create policy "users read own keys"   on public.user_api_keys for select using (auth.uid() = user_id);
create policy "users insert own keys" on public.user_api_keys for insert with check (auth.uid() = user_id);
create policy "users update own keys" on public.user_api_keys for update using (auth.uid() = user_id);
create policy "users delete own keys" on public.user_api_keys for delete using (auth.uid() = user_id);
