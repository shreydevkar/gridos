"""Per-user API keys for the Engine API (phase 8).

External AI agents need a long-lived credential to call /eval, /schema, and
/peek without going through the Supabase JWT flow (which expires hourly and
requires a real browser session). Users mint keys from the Settings UI; the
server returns the full key ONCE on creation and stores only its sha256
hash. Subsequent requests carry the key in `Authorization: Bearer
gridos_live_sk_<...>`; cloud/auth.py routes prefix-matching credentials to
`lookup_user_id_by_hash` here.

Schema in migration 0011_api_keys.sql. OSS mode is a no-op (returns empty
results / None).

Cache: lookup_user_id_by_hash maintains an in-memory TTL cache so the
authenticated hot path doesn't hit Supabase on every request. 30s TTL is
the sweet spot — short enough that a revocation propagates quickly, long
enough that an agent firing 10 req/s sees ~0.03 cache misses per second.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from threading import Lock
from typing import Optional

from cloud import config as cloud_config


KEY_PREFIX = "gridos_live_sk_"
PREFIX_DISPLAY_LEN = 16  # how many leading chars we store/show as the human-readable identifier


def _client():
    from supabase import create_client  # type: ignore

    return create_client(cloud_config.SUPABASE_URL, cloud_config.SUPABASE_SERVICE_ROLE_KEY)


def _saas_configured() -> bool:
    return bool(
        cloud_config.SAAS_MODE
        and cloud_config.SUPABASE_URL
        and cloud_config.SUPABASE_SERVICE_ROLE_KEY
    )


def _hash(full_key: str) -> str:
    return hashlib.sha256(full_key.encode("utf-8")).hexdigest()


def _generate_key() -> str:
    """gridos_live_sk_ + 32 chars of url-safe base64. ~52 chars total. Plenty
    of entropy (192 bits) and pattern-recognizable from the prefix alone for
    secret-scanners (GitHub, Gitleaks, etc.) to flag accidental commits."""
    body = secrets.token_urlsafe(24)  # 24 bytes → 32 base64 chars
    return f"{KEY_PREFIX}{body}"


# ---------- in-memory cache for the hot lookup path ----------

_CACHE_TTL_SECONDS = 30.0
_lookup_cache: dict[str, tuple[float, Optional[str]]] = {}
_lookup_cache_lock = Lock()


def _cache_get(key_hash: str) -> Optional[tuple[bool, Optional[str]]]:
    """Returns (hit, user_id_or_none) — outer hit=False means we have no
    fresh entry and the caller should query Supabase."""
    now = time.monotonic()
    with _lookup_cache_lock:
        entry = _lookup_cache.get(key_hash)
        if entry is None:
            return None
        ts, user_id = entry
        if now - ts > _CACHE_TTL_SECONDS:
            _lookup_cache.pop(key_hash, None)
            return None
        return (True, user_id)


def _cache_put(key_hash: str, user_id: Optional[str]) -> None:
    with _lookup_cache_lock:
        _lookup_cache[key_hash] = (time.monotonic(), user_id)
        # Soft cap so a flood of bad keys can't wedge memory. 1024 entries
        # is generous — corresponds to 1024 distinct keys queried inside a
        # 30s window. Far more than any real workload.
        if len(_lookup_cache) > 1024:
            for k in list(_lookup_cache.keys())[:128]:
                _lookup_cache.pop(k, None)


def invalidate_cache_for_user(user_id: str) -> None:
    """Drop every cached lookup for this user. Called on revoke so a freshly
    revoked key stops working immediately rather than after the TTL."""
    if not user_id:
        return
    with _lookup_cache_lock:
        # We don't index by user_id, so this is O(N). N is bounded by the
        # cache cap (1024) and only runs on revoke (rare). Acceptable.
        for hashed, (_ts, uid) in list(_lookup_cache.items()):
            if uid == user_id:
                _lookup_cache.pop(hashed, None)


# ---------- public API ----------


def looks_like_api_key(token: str) -> bool:
    return isinstance(token, str) and token.startswith(KEY_PREFIX)


def create_key(user_id: str, name: str) -> dict:
    """Generate a fresh key, persist its hash, return the FULL key once.

    Returns: {id, name, key, prefix, created_at}. The `key` field is the
    only place the plaintext key ever appears on the wire; the caller is
    responsible for showing it once and never asking for it again.
    """
    if not _saas_configured():
        raise RuntimeError("API keys require SaaS mode + a configured Supabase project.")
    if not user_id:
        raise ValueError("user_id is required.")

    full_key = _generate_key()
    key_hash = _hash(full_key)
    prefix = full_key[:PREFIX_DISPLAY_LEN]

    res = (
        _client()
        .table("api_keys")
        .insert({
            "user_id": user_id,
            "name": (name or "Untitled key").strip()[:80],
            "key_hash": key_hash,
            "prefix": prefix,
        })
        .execute()
    )
    rows = res.data or []
    if not rows:
        raise RuntimeError("Failed to insert API key row.")
    row = rows[0]
    return {
        "id": row["id"],
        "name": row["name"],
        "key": full_key,
        "prefix": prefix,
        "created_at": row.get("created_at"),
    }


def list_keys(user_id: str) -> list[dict]:
    """List the user's keys WITHOUT exposing the secret. Includes revoked
    keys so the audit log is visible to the user; client should grey them
    out."""
    if not _saas_configured() or not user_id:
        return []
    res = (
        _client()
        .table("api_keys")
        .select("id, name, prefix, created_at, last_used_at, revoked_at")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return list(res.data or [])


def revoke_key(user_id: str, key_id: str) -> bool:
    """Soft-revoke. Returns True if a row was updated, False if no matching
    key exists for this user (404-shaped)."""
    if not _saas_configured() or not user_id or not key_id:
        return False
    res = (
        _client()
        .table("api_keys")
        .update({"revoked_at": "now()"})
        .eq("user_id", user_id)
        .eq("id", key_id)
        .is_("revoked_at", "null")
        .execute()
    )
    rows = res.data or []
    if rows:
        invalidate_cache_for_user(user_id)
        return True
    return False


def lookup_user_id_by_hash(full_key: str) -> Optional[str]:
    """Hot-path auth lookup. Hashes the incoming bearer token and finds the
    matching unrevoked api_keys row. Cached in-process for 30s.

    Returns the owning user_id, or None if the key is unknown or revoked.
    Side effect: bumps last_used_at on cache miss (i.e. roughly every 30s
    per active key, not per request — good enough for "last seen" UI)."""
    if not _saas_configured() or not full_key:
        return None
    key_hash = _hash(full_key)

    cached = _cache_get(key_hash)
    if cached is not None:
        return cached[1]

    try:
        res = (
            _client()
            .table("api_keys")
            .select("id, user_id, revoked_at")
            .eq("key_hash", key_hash)
            .limit(1)
            .execute()
        )
    except Exception as e:
        print(f"[api_keys] lookup failed: {e}")
        return None

    rows = res.data or []
    if not rows or rows[0].get("revoked_at"):
        _cache_put(key_hash, None)
        return None

    user_id = rows[0]["user_id"]
    _cache_put(key_hash, user_id)
    # Fire-and-forget update of last_used_at. Errors are non-fatal — we don't
    # want a transient Supabase glitch to fail the user's API call just
    # because we couldn't update a timestamp.
    try:
        (
            _client()
            .table("api_keys")
            .update({"last_used_at": "now()"})
            .eq("id", rows[0]["id"])
            .execute()
        )
    except Exception as e:
        print(f"[api_keys] last_used_at bump failed: {e}")
    return user_id
