-- 0002_usage_rollup.sql — monthly usage rollup via AFTER INSERT trigger.
--
-- Python (cloud/usage.py) inserts one row into public.usage_logs per LLM call
-- with prompt/completion tokens and an estimated cost. This trigger atomically
-- folds that row into the (user_id, month) partition in public.user_usage —
-- so tier-enforcement reads never race against concurrent log inserts.
--
-- `cost_cents` on usage_logs is new; default 0 keeps historical reads safe.
-- The rollup trigger is idempotent to redeploy (drop + recreate).

alter table public.usage_logs
  add column if not exists cost_cents bigint not null default 0;

create or replace function public.usage_logs_rollup()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.user_usage (user_id, month, total_tokens, cost_cents)
  values (
    new.user_id,
    date_trunc('month', coalesce(new.created_at, now()))::date,
    coalesce(new.prompt_tokens, 0) + coalesce(new.completion_tokens, 0),
    coalesce(new.cost_cents, 0)
  )
  on conflict (user_id, month) do update
    set total_tokens = public.user_usage.total_tokens + excluded.total_tokens,
        cost_cents   = public.user_usage.cost_cents   + excluded.cost_cents;
  return new;
end;
$$;

drop trigger if exists usage_logs_rollup on public.usage_logs;

create trigger usage_logs_rollup
  after insert on public.usage_logs
  for each row
  execute function public.usage_logs_rollup();
