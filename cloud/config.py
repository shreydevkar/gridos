"""Config module — resolves the open-core vs. SaaS mode at process start.

Environment contract:
  SAAS_MODE             = "true" | "false" (default false)
  SUPABASE_URL          = https://<project>.supabase.co
  SUPABASE_KEY          = service-role key (Project Settings → API → service_role)
  SUPABASE_JWT_SECRET   = JWT signing secret (Project Settings → API → JWT Secret).
                          Distinct from SUPABASE_KEY — used for local HS256
                          verification of tokens the frontend receives from
                          Supabase Auth. Never ship this to the browser.
  STRIPE_SECRET_KEY     = sk_live_... | sk_test_... (Phase 4)
  STRIPE_WEBHOOK_SECRET = whsec_... (Phase 4)

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
SUPABASE_KEY: str | None = _env_str("SUPABASE_KEY")
SUPABASE_JWT_SECRET: str | None = _env_str("SUPABASE_JWT_SECRET")

STRIPE_SECRET_KEY: str | None = _env_str("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET: str | None = _env_str("STRIPE_WEBHOOK_SECRET")


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
        "SUPABASE_JWT_SECRET": SUPABASE_JWT_SECRET,
    }),
    "cloud_storage": _availability({"SUPABASE_URL": SUPABASE_URL, "SUPABASE_KEY": SUPABASE_KEY}),
    "billing": _availability({"STRIPE_SECRET_KEY": STRIPE_SECRET_KEY, "STRIPE_WEBHOOK_SECRET": STRIPE_WEBHOOK_SECRET}),
    "usage_tracking": _availability({"SUPABASE_URL": SUPABASE_URL, "SUPABASE_KEY": SUPABASE_KEY}),
}


def snapshot() -> dict:
    """Serializable view of the current mode + feature availability."""
    return {
        "mode": "saas" if SAAS_MODE else "oss",
        "features": {name: f.to_dict() for name, f in SAAS_FEATURES.items()},
    }
