"""Config module — resolves the open-core vs. SaaS mode at process start.

Environment contract:
  SAAS_MODE                 = "true" | "false" (default false)
  SUPABASE_URL              = https://<project>.supabase.co — safe to expose.
  SUPABASE_ANON_KEY         = anon (publishable) key (Project Settings → API →
                              anon public). The frontend uses this directly.
                              Safe to ship to the browser — RLS is the gate.
  SUPABASE_SERVICE_ROLE_KEY = service-role key (Project Settings → API →
                              service_role). Server-only — bypasses RLS.
                              Never ship this to the browser.
  SUPABASE_JWT_SECRET       = JWT signing secret (Project Settings → API →
                              JWT Secret). Used for local HS256 verification
                              of access tokens the frontend sends us. Never
                              ship this to the browser.
  STRIPE_SECRET_KEY         = sk_live_... | sk_test_... (Phase 4)
  STRIPE_WEBHOOK_SECRET     = whsec_... (Phase 4)

Backward compat: SUPABASE_KEY (singular) is still accepted and treated as the
service-role key, so existing `.env` files don't break mid-migration.

Rules:
  - Boolean parse treats "1", "true", "yes", "on" (case-insensitive) as true.
  - `SAAS_MODE=true` with a missing SUPABASE_URL/KEY is NOT a hard failure at
    import time — the import-stage failure mode would take the OSS users down
    if they ever set SAAS_MODE=true by mistake with no keys. Instead, SaaS
    features expose their missing-config state via `SAAS_FEATURES` and the
    per-feature routers raise 503 with a specific detail when hit.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_str(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    cleaned = raw.strip()
    return cleaned or None


SAAS_MODE: bool = _env_bool("SAAS_MODE", default=False)

SUPABASE_URL: str | None = _env_str("SUPABASE_URL")
SUPABASE_ANON_KEY: str | None = _env_str("SUPABASE_ANON_KEY")
# Service-role key — accept both the canonical name and the legacy SUPABASE_KEY
# alias so users with an older .env don't need to rename immediately.
SUPABASE_SERVICE_ROLE_KEY: str | None = _env_str("SUPABASE_SERVICE_ROLE_KEY") or _env_str("SUPABASE_KEY")
# Back-compat export — older cloud modules may still reference SUPABASE_KEY.
SUPABASE_KEY: str | None = SUPABASE_SERVICE_ROLE_KEY
SUPABASE_JWT_SECRET: str | None = _env_str("SUPABASE_JWT_SECRET")

STRIPE_SECRET_KEY: str | None = _env_str("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET: str | None = _env_str("STRIPE_WEBHOOK_SECRET")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


# Monthly per-tier token caps. These are **product** tier limits, not
# operator-cost controls — GridOS SaaS is BYOK so the user pays their own
# LLM bills, but each tier still gets a monthly budget of agentic tokens as
# part of the plan. Enforced in /agent/chat and /agent/chat/chain via
# cloud/usage.over_quota_check() which returns 402 at the cap; the account
# popover's progress bar renders the usage-to-cap ratio.
#
# Tier psychology:
#   Free       → try-it amount; hit a real ceiling quickly
#   Plus       → low-friction entry paid tier; removes Free's pain
#   Pro        → main plan; anchored so it looks like the sweet spot
#   Enterprise → unlimited; makes Pro look reasonable
#   Student    → Pro-level tokens with fewer workbook slots, unlocked by
#                verification (.edu email / GitHub Student Pack — enforcement
#                lands with the Stripe phase). "We trust you to do real work"
#                rather than a throttled free tier.
#
# 0 means unlimited (enterprise, and anyone overriding via env for dev).
FREE_TIER_MONTHLY_TOKENS:    int = _env_int("FREE_TIER_MONTHLY_TOKENS",      100_000)
PLUS_TIER_MONTHLY_TOKENS:    int = _env_int("PLUS_TIER_MONTHLY_TOKENS",    1_000_000)
PRO_TIER_MONTHLY_TOKENS:     int = _env_int("PRO_TIER_MONTHLY_TOKENS",     5_000_000)
STUDENT_TIER_MONTHLY_TOKENS: int = _env_int("STUDENT_TIER_MONTHLY_TOKENS", 5_000_000)

# Cloud workbook slots per tier. 0 means unlimited (enterprise).
FREE_TIER_MAX_WORKBOOKS:    int = _env_int("FREE_TIER_MAX_WORKBOOKS",     3)
PLUS_TIER_MAX_WORKBOOKS:    int = _env_int("PLUS_TIER_MAX_WORKBOOKS",    10)
PRO_TIER_MAX_WORKBOOKS:     int = _env_int("PRO_TIER_MAX_WORKBOOKS",     50)
STUDENT_TIER_MAX_WORKBOOKS: int = _env_int("STUDENT_TIER_MAX_WORKBOOKS", 25)


def tier_limit(tier: str) -> int:
    """Monthly token cap for the given subscription tier. 0 means unlimited."""
    t = (tier or "free").lower()
    if t == "enterprise":
        return 0  # unlimited
    if t == "pro":
        return PRO_TIER_MONTHLY_TOKENS
    if t == "student":
        return STUDENT_TIER_MONTHLY_TOKENS
    if t == "plus":
        return PLUS_TIER_MONTHLY_TOKENS
    return FREE_TIER_MONTHLY_TOKENS


def max_workbooks(tier: str) -> int:
    """Workbook-slot cap for the given subscription tier. 0 means unlimited."""
    t = (tier or "free").lower()
    if t == "enterprise":
        return 0
    if t == "pro":
        return PRO_TIER_MAX_WORKBOOKS
    if t == "student":
        return STUDENT_TIER_MAX_WORKBOOKS
    if t == "plus":
        return PLUS_TIER_MAX_WORKBOOKS
    return FREE_TIER_MAX_WORKBOOKS


@dataclass(frozen=True)
class FeatureAvailability:
    enabled: bool
    missing_config: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "missing_config": list(self.missing_config),
        }


def _availability(required_env: dict[str, object]) -> FeatureAvailability:
    # Only meaningful when SAAS_MODE is on; OSS mode always reports disabled
    # regardless of which env vars happen to be set.
    if not SAAS_MODE:
        return FeatureAvailability(enabled=False)
    missing = tuple(k for k, v in required_env.items() if not v)
    return FeatureAvailability(enabled=not missing, missing_config=missing)


SAAS_FEATURES: dict[str, FeatureAvailability] = {
    "auth": _availability({
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_ANON_KEY": SUPABASE_ANON_KEY,
        "SUPABASE_JWT_SECRET": SUPABASE_JWT_SECRET,
    }),
    "cloud_storage": _availability({
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_SERVICE_ROLE_KEY": SUPABASE_SERVICE_ROLE_KEY,
    }),
    "billing": _availability({
        "STRIPE_SECRET_KEY": STRIPE_SECRET_KEY,
        "STRIPE_WEBHOOK_SECRET": STRIPE_WEBHOOK_SECRET,
    }),
    "usage_tracking": _availability({
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_SERVICE_ROLE_KEY": SUPABASE_SERVICE_ROLE_KEY,
    }),
}


def public_client_config() -> dict:
    """Config values the browser is allowed to see. Only surfaced in SaaS mode
    so OSS responses don't advertise a non-existent Supabase project."""
    if not SAAS_MODE:
        return {}
    return {
        "supabase_url": SUPABASE_URL,
        "supabase_anon_key": SUPABASE_ANON_KEY,
    }


def snapshot() -> dict:
    """Serializable view of the current mode + feature availability. In SaaS
    mode also bundles the public client config so the frontend can init the
    Supabase JS client on bootstrap."""
    out = {
        "mode": "saas" if SAAS_MODE else "oss",
        "features": {name: f.to_dict() for name, f in SAAS_FEATURES.items()},
    }
    if SAAS_MODE:
        out["client_config"] = public_client_config()
    return out
