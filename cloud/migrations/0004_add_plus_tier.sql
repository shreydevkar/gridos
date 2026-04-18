-- Add 'plus' to the allowed subscription_tier values.
--
-- Existing check constraint was: subscription_tier IN ('free','pro','enterprise').
-- We insert 'plus' between Free and Pro as the low-friction entry paid tier
-- (see cloud/config.py PLUS_TIER_* constants). No row updates — existing
-- users stay on their current tier until manually migrated or Stripe upgrades.

alter table public.users
    drop constraint if exists users_subscription_tier_check;

alter table public.users
    add constraint users_subscription_tier_check
    check (subscription_tier = any (array['free'::text, 'plus'::text, 'pro'::text, 'enterprise'::text]));
