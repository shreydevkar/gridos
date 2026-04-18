"""WorkbookStore — the persistence seam between OSS and SaaS.

Endpoints don't branch on SAAS_MODE; they call `store.load(scope)` /
`store.save(scope, state_dict)` and `main.py` picks the right implementation
at startup. `FileWorkbookStore` is the OSS default (flat file on disk, same
behavior as before). `cloud.supabase_store.SupabaseWorkbookStore` is the SaaS
impl.

`state_dict` is whatever `GridOSKernel.export_state_dict()` produces — the
store is format-agnostic; it only ferries bytes.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol


@dataclass(frozen=True)
class WorkbookScope:
    """Identifies which workbook a request operates on.

    OSS: `user_id` is None, `workbook_id` is always "default" — the server
        holds a single workbook at a time (`system_state.gridos`).
    SaaS: `user_id` comes from the authenticated JWT; `workbook_id` is the
        uuid of whichever workbook the session has open.
    """
    user_id: Optional[str]
    workbook_id: str = "default"


class WorkbookStore(Protocol):
    def load(self, scope: WorkbookScope) -> Optional[dict]:
        """Return the serialized state dict, or None if nothing saved yet."""
        ...

    def save(self, scope: WorkbookScope, state_dict: dict) -> None:
        """Persist the full state dict. Implementations upsert."""
        ...

    def list(self, user_id: Optional[str]) -> list[dict]:
        """Return `[{id, title, updated_at}]` for every workbook the user owns."""
        ...

    def delete(self, scope: WorkbookScope) -> None:
        """Remove a workbook. No-op if it doesn't exist."""
        ...


class FileWorkbookStore:
    """OSS store. Flat files under `base_dir`; "default" maps to the legacy
    `system_state.gridos` filename so existing saves are picked up without
    migration. `user_id` is ignored — OSS is single-user by design."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or Path.cwd()

    def _path_for(self, scope: WorkbookScope) -> Path:
        if scope.workbook_id == "default":
            return self.base_dir / "system_state.gridos"
        # Very defensive filename — workbook_id should be uuid-shaped but
        # we treat it as opaque and strip path separators.
        safe = scope.workbook_id.replace("/", "_").replace("\\", "_")
        return self.base_dir / f"{safe}.gridos"

    def load(self, scope: WorkbookScope) -> Optional[dict]:
        path = self._path_for(scope)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def save(self, scope: WorkbookScope, state_dict: dict) -> None:
        path = self._path_for(scope)
        path.write_text(json.dumps(state_dict, indent=2), encoding="utf-8")

    def list(self, user_id: Optional[str]) -> list[dict]:
        # OSS is single-workbook: just report whether the default file exists.
        default = self.base_dir / "system_state.gridos"
        if not default.exists():
            return []
        stat = default.stat()
        return [{
            "id": "default",
            "title": "Default workbook",
            "updated_at": stat.st_mtime,
        }]

    def delete(self, scope: WorkbookScope) -> None:
        path = self._path_for(scope)
        if path.exists():
            path.unlink()
