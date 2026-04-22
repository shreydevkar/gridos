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

    # ---- Multi-workbook helpers (Phase 5) ---------------------------------

    def count(self, user_id: str) -> int:
        """Return how many workbooks the user owns — used by the free-tier
        slot cap. `select('id', count='exact')` sends a HEAD-style count so
        we don't pay to ship every row back."""
        if not user_id:
            raise SupabaseAuthError("count requires an authenticated user_id.")
        res = (
            self._client.table("workbooks")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .execute()
        )
        return int(getattr(res, "count", 0) or 0)

    def create_empty(self, user_id: str, title: str) -> dict:
        """Insert a blank workbook row and return {id, title, updated_at}.
        The caller will typically redirect to the workbook view with this id
        in the URL so the kernel loads (and re-saves) against it."""
        if not user_id:
            raise SupabaseAuthError("create_empty requires an authenticated user_id.")
        safe_title = (title or "").strip()[:120] or "Untitled workbook"
        initial_state = {
            "workbook_name": safe_title,
            "active_sheet": "Sheet1",
            "sheet_order": ["Sheet1"],
            "sheets": {"Sheet1": {"cells": {}, "charts": []}},
            "chat_log": [],
        }
        res = (
            self._client.table("workbooks")
            .insert({
                "user_id": user_id,
                "title": safe_title,
                "grid_state": initial_state,
            })
            .execute()
        )
        rows = res.data or []
        if not rows:
            raise RuntimeError("create_empty: Supabase returned no row from insert.")
        row = rows[0]
        return {
            "id": row.get("id"),
            "title": row.get("title") or safe_title,
            "updated_at": row.get("updated_at"),
        }

    def rename(self, scope: WorkbookScope, new_title: str) -> None:
        """Update both the title column and the nested workbook_name in
        grid_state so a subsequent load sees a consistent name."""
        user_id = self._require_user(scope)
        safe_title = (new_title or "").strip()[:120] or "Untitled workbook"
        # Read the current grid_state so we can patch workbook_name in-place.
        existing = (
            self._client.table("workbooks")
            .select("grid_state")
            .eq("user_id", user_id)
            .eq("id", scope.workbook_id)
            .limit(1)
            .execute()
        )
        rows = existing.data or []
        if not rows:
            return
        state = rows[0].get("grid_state") or {}
        state["workbook_name"] = safe_title
        (
            self._client.table("workbooks")
            .update({"title": safe_title, "grid_state": state})
            .eq("user_id", user_id)
            .eq("id", scope.workbook_id)
            .execute()
        )

    # ---- Sharing (Phase 7) ------------------------------------------------

    def resolve_workbook_access(self, user_id: str, workbook_id: str) -> Optional[dict]:
        """Return {'owner_id': uuid, 'role': 'owner'|'editor'|'viewer'} if this
        user may access this workbook, else None.

        The API layer calls this before routing a request to a kernel. Owner
        → role='owner' (full permissions). Collaborator → whatever role the
        grant carries. Unknown pair → None (→ 404 from the caller)."""
        if not user_id or not workbook_id:
            return None
        row = (
            self._client.table("workbooks")
            .select("id, user_id")
            .eq("id", workbook_id)
            .limit(1)
            .execute()
        )
        workbook_rows = row.data or []
        if not workbook_rows:
            return None
        owner_id = workbook_rows[0]["user_id"]
        if owner_id == user_id:
            return {"owner_id": owner_id, "role": "owner"}
        # Not the owner — check the collaborator table.
        collab = (
            self._client.table("workbook_collaborators")
            .select("role")
            .eq("workbook_id", workbook_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        collab_rows = collab.data or []
        if not collab_rows:
            return None
        return {"owner_id": owner_id, "role": collab_rows[0]["role"]}

    def list_collaborators(self, workbook_id: str) -> list[dict]:
        """Return every collaborator on this workbook with their email. Used
        by the owner-facing Share… modal to show who has access.

        Shape: [{user_id, email, role, invited_at, accepted_at}].

        Two queries instead of a PostgREST embedded select — the
        workbook_collaborators table has two FKs to public.users (user_id
        and invited_by), which makes `users(email)` ambiguous and errors
        out with a 500. Two queries keeps it unambiguous and is cheap —
        collaborator counts are tiny."""
        if not workbook_id:
            return []
        rows_res = (
            self._client.table("workbook_collaborators")
            .select("user_id, role, invited_at, accepted_at")
            .eq("workbook_id", workbook_id)
            .order("invited_at", desc=False)
            .execute()
        )
        rows = rows_res.data or []
        if not rows:
            return []
        user_ids = list({r["user_id"] for r in rows if r.get("user_id")})
        email_by_id: dict[str, str] = {}
        if user_ids:
            emails_res = (
                self._client.table("users")
                .select("id, email")
                .in_("id", user_ids)
                .execute()
            )
            for u in emails_res.data or []:
                if u.get("id"):
                    email_by_id[u["id"]] = u.get("email") or ""
        return [
            {
                "user_id": r.get("user_id"),
                "email": email_by_id.get(r.get("user_id", ""), ""),
                "role": r.get("role"),
                "invited_at": r.get("invited_at"),
                "accepted_at": r.get("accepted_at"),
            }
            for r in rows
        ]

    def add_collaborator_by_email(
        self,
        workbook_id: str,
        inviter_id: str,
        email: str,
        role: str,
    ) -> dict:
        """Invite a user by email. Requires the invitee to already have a
        GridOS account (public.users row). Returns the grant row.

        Raises LookupError if the email isn't registered, ValueError on bad
        inputs. Upserts so re-inviting the same email just updates the role
        instead of erroring."""
        if not workbook_id or not inviter_id or not email:
            raise ValueError("workbook_id, inviter_id, and email are required.")
        if role not in ("editor", "viewer"):
            raise ValueError(f"role must be 'editor' or 'viewer', got {role!r}")
        target = (
            self._client.table("users")
            .select("id, email")
            .eq("email", email.strip().lower())
            .limit(1)
            .execute()
        )
        target_rows = target.data or []
        if not target_rows:
            raise LookupError(f"No GridOS account found for {email}")
        target_id = target_rows[0]["id"]
        if target_id == inviter_id:
            raise ValueError("Cannot invite yourself.")
        payload = {
            "workbook_id": workbook_id,
            "user_id": target_id,
            "role": role,
            "invited_by": inviter_id,
        }
        (
            self._client.table("workbook_collaborators")
            .upsert(payload, on_conflict="workbook_id,user_id")
            .execute()
        )
        return {
            "user_id": target_id,
            "email": target_rows[0]["email"],
            "role": role,
        }

    def remove_collaborator(self, workbook_id: str, user_id: str) -> None:
        """Revoke access. No-op if the grant doesn't exist."""
        if not workbook_id or not user_id:
            return
        (
            self._client.table("workbook_collaborators")
            .delete()
            .eq("workbook_id", workbook_id)
            .eq("user_id", user_id)
            .execute()
        )

    def list_shared_with_user(self, user_id: str) -> list[dict]:
        """Workbooks shared with this user (not owned by them). Returns
        [{id, title, updated_at, owner_email, role}] for the Load modal."""
        if not user_id:
            return []
        res = (
            self._client.table("workbook_collaborators")
            .select("role, workbooks(id, title, updated_at, users(email))")
            .eq("user_id", user_id)
            .execute()
        )
        out = []
        for r in res.data or []:
            wb = r.get("workbooks") or {}
            owner = wb.get("users") or {}
            if not wb.get("id"):
                continue
            out.append({
                "id": wb["id"],
                "title": wb.get("title") or "Untitled workbook",
                "updated_at": wb.get("updated_at"),
                "owner_email": owner.get("email"),
                "role": r.get("role"),
            })
        # Sort newest-first to match the owned-workbooks list ordering.
        out.sort(key=lambda d: d.get("updated_at") or "", reverse=True)
        return out
