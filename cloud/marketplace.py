"""Per-user plugin marketplace (SaaS).

Plugins are shipped in the repo's `plugins/` directory and loaded globally
at server boot (see core/plugins.py). This module tracks which of those
plugins each user has *installed* into their working system — purely a
discovery/visibility layer for V0, not a sandbox. The installed formulas
remain globally callable regardless; the marketplace's job is to let a
user curate what they see and (in future) drive per-user agent-prompt
composition.

OSS mode: there is no per-user state, so `list_installed` returns
everything and `set_installed` is a no-op.
"""
from __future__ import annotations

from typing import Iterable, Optional

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


def list_installed(user_id: str) -> set[str]:
    """Return the set of plugin slugs the user has installed. In OSS mode
    (or on any failure) returns an empty set — the caller should treat that
    as 'nothing explicitly selected' and typically fall back to showing all
    available plugins as uninstalled."""
    if not _saas_configured() or not user_id:
        return set()
    try:
        res = (
            _client()
            .table("user_plugins")
            .select("plugin_slug, enabled")
            .eq("user_id", user_id)
            .execute()
        )
    except Exception as e:
        print(f"[marketplace] list failed for {user_id}: {e}")
        return set()
    return {r["plugin_slug"] for r in (res.data or []) if r.get("enabled")}


def set_installed(user_id: str, plugin_slug: str, installed: bool) -> None:
    """Install (upsert enabled=true) or uninstall (delete row) a plugin for
    the given user."""
    if not _saas_configured() or not user_id:
        raise RuntimeError("set_installed requires SaaS mode with Supabase configured.")
    if installed:
        _client().table("user_plugins").upsert(
            {"user_id": user_id, "plugin_slug": plugin_slug, "enabled": True},
            on_conflict="user_id,plugin_slug",
        ).execute()
    else:
        (
            _client()
            .table("user_plugins")
            .delete()
            .eq("user_id", user_id)
            .eq("plugin_slug", plugin_slug)
            .execute()
        )


def annotate_manifests(manifests: list[dict], installed_slugs: Iterable[str]) -> list[dict]:
    """Return a new list with `installed: bool` stamped on each manifest.
    Caller owns freshness of `installed_slugs` (e.g. result of list_installed)."""
    installed = set(installed_slugs)
    return [{**m, "installed": m["slug"] in installed} for m in manifests]
