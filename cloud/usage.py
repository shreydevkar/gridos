"""Per-call usage logging — writes every LLM invocation to public.usage_logs.

The monthly rollup into public.user_usage is done server-side by an AFTER
INSERT trigger (see cloud/migrations/0002_usage_rollup.sql), so Python only
does the single insert. Quota reads can trust user_usage without racing.

Scope is threaded through async requests via ContextVars — call_model() in
main.py doesn't take user_id/workbook_id params; instead each endpoint that
might produce LLM calls binds the context at the top with
`set_request_context(user.id, workbook_id)`. Any call_model() reached within
that request picks it up.

Logging is best-effort: a Supabase error MUST NOT fail the underlying user
request. Every write is try/excepted and the failure is logged-and-dropped.
"""
from __future__ import annotations

import contextvars
import logging
from datetime import datetime, timezone
from typing import Optional

from cloud import config

log = logging.getLogger(__name__)

_USER_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "gridos_usage_user_id", default=None
)
_WORKBOOK_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "gridos_usage_workbook_id", default=None
)

_CLIENT = None
_CLIENT_INIT_FAILED = False


def _client():
    """Lazy Supabase client. Uses the service-role key — we're inserting into
    usage_logs and users' own RLS would block us otherwise. None is returned
    when tracking is disabled or supabase-py isn't installed; callers treat
    that as "log nothing, no error"."""
    global _CLIENT, _CLIENT_INIT_FAILED
    if _CLIENT is not None:
        return _CLIENT
    if _CLIENT_INIT_FAILED:
        return None
    if not (config.SAAS_MODE and config.SAAS_FEATURES["usage_tracking"].enabled):
        return None
    try:
        from supabase import create_client  # type: ignore
    except ImportError:
        log.warning("[usage] supabase-py not installed; usage logging disabled.")
        _CLIENT_INIT_FAILED = True
        return None
    try:
        _CLIENT = create_client(
            config.SUPABASE_URL, config.SUPABASE_SERVICE_ROLE_KEY
        )
    except Exception as e:
        log.warning("[usage] Supabase client init failed: %s", e)
        _CLIENT_INIT_FAILED = True
        return None
    return _CLIENT


def set_request_context(
    user_id: Optional[str], workbook_id: Optional[str]
) -> None:
    """Bind the current async task's user + workbook scope. Safe to call with
    None or the OSS sentinel 'oss' — log_call is a no-op in those cases."""
    _USER_ID.set(user_id)
    _WORKBOOK_ID.set(workbook_id)


def clear_request_context() -> None:
    _USER_ID.set(None)
    _WORKBOOK_ID.set(None)


# Rough per-million-token pricing in USD. Overestimate is fine — this is
# a ballpark meter for quota enforcement, not an invoice. Longer prefix
# wins, so "claude-opus" matches before the ""-wildcard fallback.
_PRICING_USD_PER_MTOK: dict[tuple[str, str], tuple[float, float]] = {
    ("anthropic", "claude-opus"): (15.0, 75.0),
    ("anthropic", "claude-sonnet"): (3.0, 15.0),
    ("anthropic", "claude-haiku"): (1.0, 5.0),
    ("anthropic", ""): (3.0, 15.0),
    ("groq", ""): (0.10, 0.20),
    ("openrouter", ""): (1.0, 3.0),
    ("google", ""): (1.25, 5.0),
    ("gemini", ""): (1.25, 5.0),
}
_DEFAULT_RATE = (2.0, 6.0)


def _estimate_cost_cents(
    provider: str, model: str, prompt_tokens: int, completion_tokens: int
) -> int:
    prompt_rate, comp_rate = _DEFAULT_RATE
    best = -1
    for (p, m_prefix), rates in _PRICING_USD_PER_MTOK.items():
        if p == provider and model.startswith(m_prefix) and len(m_prefix) > best:
            prompt_rate, comp_rate = rates
            best = len(m_prefix)
    cost_usd = (
        (prompt_tokens or 0) / 1_000_000 * prompt_rate
        + (completion_tokens or 0) / 1_000_000 * comp_rate
    )
    return int(round(cost_usd * 100))


def log_call(
    *,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    finish_reason: Optional[str] = None,
) -> None:
    """Record an LLM call for the current request's authenticated user.
    No-op when: not SaaS mode, tracking feature disabled, client init failed,
    unauthenticated request, or OSS sentinel user. Swallows all errors."""
    client = _client()
    if client is None:
        return

    user_id = _USER_ID.get()
    if not user_id or user_id == "oss":
        return

    workbook_id = _WORKBOOK_ID.get()
    cost_cents = _estimate_cost_cents(
        provider, model, prompt_tokens, completion_tokens
    )

    row = {
        "user_id": user_id,
        "provider": provider,
        "model": model,
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "finish_reason": finish_reason,
        "workbook_id": workbook_id,
        "cost_cents": cost_cents,
    }
    try:
        client.table("usage_logs").insert(row).execute()
    except Exception as e:
        log.warning("[usage] usage_logs insert failed for %s: %s", user_id, e)


# ---------- Quota enforcement (Phase 4b) ------------------------------------
# Read the authenticated user's current-month token usage + their
# subscription tier from the service-role client, compare against the per-tier
# cap in cloud.config.tier_limit, and return a tuple the endpoint can use to
# either proceed or raise 402.
#
# Quota checks happen BEFORE a chat starts; once a chat/chain is in flight it
# runs to completion even if it tips over. That avoids a cliff where a chain's
# final iteration would crash mid-stream, which would leave the workbook in a
# half-applied state. Hard cutoffs belong to the outer request, not mid-call.


def get_tier_and_usage(user_id: str) -> dict:
    """Return the user's tier + month-to-date usage in a single round-trip pair.
    Returns dict with keys: tier, total_tokens, cost_cents, limit, month.
    'limit' of 0 means unlimited."""
    if not user_id or user_id == "oss":
        # OSS — treat as unlimited.
        return {
            "tier": "oss",
            "total_tokens": 0,
            "cost_cents": 0,
            "limit": 0,
            "month": datetime.now(timezone.utc).strftime("%Y-%m-01"),
        }

    client = _client()
    month_str = datetime.now(timezone.utc).strftime("%Y-%m-01")
    tier = "free"
    total_tokens = 0
    cost_cents = 0

    if client is not None:
        try:
            u = (
                client.table("users")
                .select("subscription_tier")
                .eq("id", user_id)
                .limit(1)
                .execute()
            )
            if u.data:
                tier = u.data[0].get("subscription_tier") or "free"
        except Exception as e:
            log.warning("[usage] users.select failed for %s: %s", user_id, e)

        try:
            r = (
                client.table("user_usage")
                .select("total_tokens, cost_cents")
                .eq("user_id", user_id)
                .eq("month", month_str)
                .limit(1)
                .execute()
            )
            if r.data:
                total_tokens = int(r.data[0].get("total_tokens") or 0)
                cost_cents = int(r.data[0].get("cost_cents") or 0)
        except Exception as e:
            log.warning("[usage] user_usage.select failed for %s: %s", user_id, e)

    return {
        "tier": tier,
        "total_tokens": total_tokens,
        "cost_cents": cost_cents,
        "limit": config.tier_limit(tier),
        "month": month_str,
    }


class QuotaExceeded(Exception):
    """Raised by over_quota_check. Endpoints translate this to HTTP 402."""

    def __init__(self, summary: dict):
        self.summary = summary
        super().__init__(
            f"Monthly token cap reached for tier '{summary.get('tier')}' "
            f"({summary.get('total_tokens')}/{summary.get('limit')})."
        )


def over_quota_check(user_id: str) -> dict:
    """Raises QuotaExceeded when the user is at or over their monthly cap.
    Returns the usage summary dict on success so callers can surface it."""
    summary = get_tier_and_usage(user_id)
    limit = int(summary.get("limit") or 0)
    if limit > 0 and int(summary.get("total_tokens") or 0) >= limit:
        raise QuotaExceeded(summary)
    return summary
