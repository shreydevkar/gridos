-- Add 'student' to the allowed subscription_tier values.
--
-- Student tier gets Pro-level tokens (5M/month) with a lower workbook cap
-- (25). Intended to be unlocked by .edu email / GitHub Student Pack
-- verification when that enforcement lands alongside Stripe.

alter table public.users
    drop constraint if exists users_subscription_tier_check;

alter table public.users
    add constraint users_subscription_tier_check
    check (subscription_tier = any (array[
        'free'::text,
        'plus'::text,
        'student'::text,
        'pro'::text,
        'enterprise'::text
    ]));
