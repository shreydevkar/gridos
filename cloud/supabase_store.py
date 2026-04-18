"""Supabase-backed WorkbookStore — used only when SAAS_MODE=true.

Reads/writes `public.workbooks.grid_state` (jsonb). Connects via supabase-py,
which is lazy-imported inside `__init__` so OSS installs don't need the
dependency pinned.

Server uses the service-role key — RLS is the user-facing safety net; the
server itself has free reign. Callers (endpoints) are responsible for
supplying a `user_id` in the scope that matches the authenticated JWT, so RLS
would still kick in if the service key ever leaked to a client build.

Every write upserts so save is idempotent. `load` returns None when the row
doesn't exist so the endpoint can fall through to "new empty workbook".
"""
from __future__ import annotations

from typing import Optional

from core.workbook_store import WorkbookScope


class SupabaseAuthError(RuntimeError):
    """Raised when the scope's user_id is missing — we never persist
    without knowing whose workbook it is."""


class SupabaseWorkbookStore:
    """Drop-in for WorkbookStore against a Supabase Postgres project.

    Not thread-safe — one client per worker is fine for FastAPI/uvicorn's
    single-process default; switch to a pool if we ever run --workers > 1.
    """

    def __init__(self, url: str, key: str):
        # Lazy import — OSS deploys don't need supabase-py installed.
        try:
            from supabase import create_client  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "supabase-py is required for SupabaseWorkbookStore. "
                "Install with: pip install supabase"
            ) from e
        self._client = create_client(url, key)

    def _require_user(self, scope: WorkbookScope) -> str:
        if not scope.user_id:
            raise SupabaseAuthError("SaaS persistence requires an authenticated user_id in scope.")
        return scope.user_id

    def load(self, scope: WorkbookScope) -> Optional[dict]:
        user_id = self._require_user(scope)
        res = (
            self._client.table("workbooks")
            .select("grid_state")
            .eq("user_id", user_id)
            .eq("id", scope.workbook_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return None
        return rows[0].get("grid_state") or None

    def save(self, scope: WorkbookScope, state_dict: dict) -> None:
        user_id = self._require_user(scope)
        title = state_dict.get("workbook_name") or "Untitled workbook"
        payload = {
            "id": scope.workbook_id,
            "user_id": user_id,
            "title": title,
            "grid_state": state_dict,
        }
        # Upsert on primary key (id). Supabase-py returns empty data on success.
        self._client.table("workbooks").upsert(payload, on_conflict="id").execute()

    def list(self, user_id: Optional[str]) -> list[dict]:
        if not user_id:
            raise SupabaseAuthError("SaaS list requires an authenticated user_id.")
        res = (
            self._client.table("workbooks")
            .select("id, title, updated_at")
            .eq("user_id", user_id)
            .order("updated_at", desc=True)
            .execute()
        )
        return res.data or []

    def delete(self, scope: WorkbookScope) -> None:
        user_id = self._require_user(scope)
        (
            self._client.table("workbooks")
            .delete()
            .eq("user_id", user_id)
            .eq("id", scope.workbook_id)
            .execute()
        )
