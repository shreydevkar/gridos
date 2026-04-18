"""Per-user LLM API-key storage for SaaS (Bring Your Own Key).

In SaaS mode, each user provides their own Gemini/Anthropic/Groq/OpenRouter
keys via the Settings panel; rows live in `public.user_api_keys` behind RLS.
The operator never pays LLM bills — the product is GridOS itself (cloud
save, multi-workbook, agentic UX), not the tokens.

OSS mode doesn't touch this module — main.py's existing disk-backed
`data/api_keys.json` remains the single source of truth for local installs.

The server uses the Supabase service-role client (bypasses RLS), so the
RLS policies on the table are defense-in-depth only: if the service key
ever leaked to a browser build, a user still couldn't read another user's
keys through a direct REST query.
"""
from __future__ import annotations

from typing import Dict

from cloud import config as cloud_config


def _client():
    """Lazy service-role client. Callers must handle supabase import errors;
    _saas_configured() above any caller prevents this from firing in OSS."""
    from supabase import create_client  # type: ignore

    return create_client(cloud_config.SUPABASE_URL, cloud_config.SUPABASE_SERVICE_ROLE_KEY)


def _saas_configured() -> bool:
    """True when SaaS mode is on AND Supabase credentials are wired. Callers
    should bail early when False — we never want to fake BYOK in OSS."""
    return bool(
        cloud_config.SAAS_MODE
        and cloud_config.SUPABASE_URL
        and cloud_config.SUPABASE_SERVICE_ROLE_KEY
    )


def list_keys(user_id: str) -> Dict[str, str]:
    """Return {provider_id: api_key} for the user. Empty dict if none set
    or on any failure — callers treat empty as 'no providers configured'
    and surface the Settings prompt in the UI."""
    if not _saas_configured() or not user_id:
        return {}
    try:
        res = (
            _client()
            .table("user_api_keys")
            .select("provider_id, api_key")
            .eq("user_id", user_id)
            .execute()
        )
    except Exception as e:
        print(f"[user_keys] list failed for {user_id}: {e}")
        return {}
    return {r["provider_id"]: r["api_key"] for r in (res.data or []) if r.get("api_key")}


def set_key(user_id: str, provider_id: str, api_key: str) -> None:
    """Upsert (user_id, provider_id) → api_key. Callers validate the key
    shape (and instantiate the provider) before calling us so invalid keys
    never hit the table."""
    if not _saas_configured() or not user_id:
        raise RuntimeError("set_key requires SaaS mode with Supabase configured.")
    payload = {
        "user_id": user_id,
        "provider_id": provider_id,
        "api_key": api_key,
    }
    _client().table("user_api_keys").upsert(payload, on_conflict="user_id,provider_id").execute()


def delete_key(user_id: str, provider_id: str) -> None:
    if not _saas_configured() or not user_id:
        raise RuntimeError("delete_key requires SaaS mode with Supabase configured.")
    (
        _client()
        .table("user_api_keys")
        .delete()
        .eq("user_id", user_id)
        .eq("provider_id", provider_id)
        .execute()
    )
