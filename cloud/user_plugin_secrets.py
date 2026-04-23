"""Per-user plugin-secret store (SaaS).

Plugins need credentials to hit third-party APIs on behalf of the signed-in
user — Shopify domain + admin token, Stripe secret key, optional GITHUB_TOKEN,
etc. The schema lives in migration 0010_user_plugin_secrets.sql; this module
is the thin CRUD layer the Settings UI + per-request resolver talk to.

OSS mode has no per-user state, so every function here is a safe no-op
(returns empty dicts / False). Plugins fall through to os.environ via the
resolver in core/plugins.py.
"""
from __future__ import annotations

from typing import Dict, Iterable

from cloud import config as cloud_config


def _client():
    from supabase import create_client  # type: ignore

    return create_client(cloud_config.SUPABASE_URL, cloud_config.SUPABASE_SERVICE_ROLE_KEY)


def _saas_configured() -> bool:
    return bool(
        cloud_config.SAAS_MODE
        and cloud_config.SUPABASE_URL
        and cloud_config.SUPABASE_SERVICE_ROLE_KEY
    )


def get_all_for(user_id: str) -> Dict[str, Dict[str, str]]:
    """Return every stored secret for this user, grouped by plugin_slug.

    Shape: {"shopify": {"STORE_DOMAIN": "...", "ADMIN_TOKEN": "..."},
            "stripe":  {"SECRET_KEY":   "..."}}.

    Used by current_kernel_dep to stuff the caller's secrets into a
    ContextVar so plugin formulas can resolve them without each formula
    re-querying Supabase."""
    if not _saas_configured() or not user_id:
        return {}
    try:
        res = (
            _client()
            .table("user_plugin_secrets")
            .select("plugin_slug, secret_key, secret_value")
            .eq("user_id", user_id)
            .execute()
        )
    except Exception as e:
        print(f"[plugin_secrets] get_all_for({user_id}) failed: {e}")
        return {}
    grouped: Dict[str, Dict[str, str]] = {}
    for row in res.data or []:
        slug = row.get("plugin_slug")
        key = row.get("secret_key")
        val = row.get("secret_value")
        if not slug or not key:
            continue
        grouped.setdefault(slug, {})[key] = val or ""
    return grouped


def list_set_keys(user_id: str, plugin_slug: str) -> list[str]:
    """Return the NAMES of keys the user has set for this plugin, never
    the values. Used by the Settings UI to show which slots are filled
    without ever shipping the secret back down."""
    if not _saas_configured() or not user_id or not plugin_slug:
        return []
    try:
        res = (
            _client()
            .table("user_plugin_secrets")
            .select("secret_key")
            .eq("user_id", user_id)
            .eq("plugin_slug", plugin_slug)
            .execute()
        )
    except Exception as e:
        print(f"[plugin_secrets] list_set_keys failed: {e}")
        return []
    return [r["secret_key"] for r in (res.data or []) if r.get("secret_key")]


def upsert_many(user_id: str, plugin_slug: str, secrets: Dict[str, str]) -> None:
    """Write or update a batch of secret_key → secret_value pairs for this
    user + plugin. Empty-string values DELETE the matching row (so the UI
    can clear a field by posting it blank). Unset keys stay untouched."""
    if not _saas_configured() or not user_id or not plugin_slug:
        return
    to_upsert = []
    to_delete: list[str] = []
    for key, value in (secrets or {}).items():
        if not key:
            continue
        if value is None or value == "":
            to_delete.append(key)
        else:
            to_upsert.append({
                "user_id": user_id,
                "plugin_slug": plugin_slug,
                "secret_key": key,
                "secret_value": value,
            })
    if to_upsert:
        (
            _client()
            .table("user_plugin_secrets")
            .upsert(to_upsert, on_conflict="user_id,plugin_slug,secret_key")
            .execute()
        )
    if to_delete:
        (
            _client()
            .table("user_plugin_secrets")
            .delete()
            .eq("user_id", user_id)
            .eq("plugin_slug", plugin_slug)
            .in_("secret_key", to_delete)
            .execute()
        )


def delete_all_for(user_id: str, plugin_slug: str) -> None:
    """Wipe every secret the user has for a plugin — used by a "Disconnect"
    button in the Settings UI."""
    if not _saas_configured() or not user_id or not plugin_slug:
        return
    (
        _client()
        .table("user_plugin_secrets")
        .delete()
        .eq("user_id", user_id)
        .eq("plugin_slug", plugin_slug)
        .execute()
    )
