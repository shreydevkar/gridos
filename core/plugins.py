"""V0 plugin loader.

A plugin is a directory under `plugins/` containing:
  - `plugin.py`     — defines a `register(kernel)` function
  - `manifest.json` — name/description/category/author/version (optional but
                      required for the SaaS marketplace to surface the plugin)

`register(kernel)` receives a `PluginKernel` and calls any combination of:
  - `@kernel.formula("NAME")` on a callable → registers a GridOS formula
  - `kernel.agent({...})`                    → registers an additional agent
  - `kernel.model({...})`                    → extends MODEL_CATALOG

Trust model: plugins run in-process with full Python access. The loader is
gated by the `GRIDOS_PLUGINS_ENABLED` env var (default on for OSS, off for
SaaS) and each plugin is isolated at import time — one plugin's failure
never aborts the boot. For SaaS the marketplace layers a per-user enabled
set on top (see cloud/marketplace.py).
"""
from __future__ import annotations

import importlib.util
import json
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from core.functions import _REGISTRY as FORMULA_REGISTRY


@dataclass
class PluginRecord:
    slug: str
    name: str
    description: str = ""
    category: str = "utility"
    author: str = ""
    version: str = "0.0.1"
    formulas: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "author": self.author,
            "version": self.version,
            "formulas": list(self.formulas),
            "agents": list(self.agents),
            "models": list(self.models),
        }


class PluginKernel:
    """Registration facade passed to each plugin's `register(kernel)`.

    Methods mutate the global formula registry (so formulas become callable
    from any cell) and collect agents/models for the caller to merge into the
    orchestration layer.
    """

    def __init__(self):
        self.records: list[PluginRecord] = []
        self.errors: list[dict] = []
        self.agents: dict[str, dict] = {}
        self.models: list[dict] = []
        self._current: Optional[PluginRecord] = None

    def formula(self, name: Optional[str] = None):
        def decorator(func: Callable) -> Callable:
            key = (name or func.__name__).upper()
            FORMULA_REGISTRY[key] = func
            if self._current is not None:
                self._current.formulas.append(key)
            return func
        return decorator

    def agent(self, spec: dict) -> dict:
        if "id" not in spec or "system_prompt" not in spec:
            raise ValueError("Plugin agent spec requires 'id' and 'system_prompt'")
        agent_id = spec["id"]
        self.agents[agent_id] = spec
        if self._current is not None:
            self._current.agents.append(agent_id)
        return spec

    def model(self, entry: dict) -> dict:
        for required in ("id", "provider", "display_name", "description"):
            if required not in entry:
                raise ValueError(f"Plugin model entry missing '{required}'")
        self.models.append(entry)
        if self._current is not None:
            self._current.models.append(entry["id"])
        return entry


def load_manifests(plugins_dir: Path) -> list[dict]:
    """Read plugin manifests without importing the plugin modules. Safe to
    call from request handlers — no side effects."""
    out: list[dict] = []
    if not plugins_dir.exists():
        return out
    for entry in sorted(plugins_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        manifest_path = entry / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        data["slug"] = entry.name
        data.setdefault("name", entry.name)
        data.setdefault("description", "")
        data.setdefault("category", "utility")
        data.setdefault("author", "")
        data.setdefault("version", "0.0.1")
        out.append(data)
    return out


def discover_and_load(plugins_dir: Path, only: Optional[set[str]] = None) -> PluginKernel:
    """Walk `plugins_dir/<slug>/plugin.py`, import each, call `register(kernel)`.

    If `only` is provided, skip plugins whose slug isn't in the set — used by
    the SaaS marketplace to honor per-user enabled lists. In OSS mode pass None
    to load everything.
    """
    kernel = PluginKernel()
    if not plugins_dir.exists() or not plugins_dir.is_dir():
        return kernel

    for entry in sorted(plugins_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        if only is not None and entry.name not in only:
            continue
        plugin_file = entry / "plugin.py"
        if not plugin_file.exists():
            continue

        record = PluginRecord(slug=entry.name, name=entry.name)
        manifest_path = entry / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                record.name = manifest.get("name", entry.name)
                record.description = manifest.get("description", "")
                record.category = manifest.get("category", "utility")
                record.author = manifest.get("author", "")
                record.version = manifest.get("version", "0.0.1")
            except (json.JSONDecodeError, OSError) as e:
                kernel.errors.append({"plugin": entry.name, "error": f"manifest: {e}"})
                continue

        module_name = f"_gridos_plugin_{entry.name}"
        spec = importlib.util.spec_from_file_location(module_name, plugin_file)
        if spec is None or spec.loader is None:
            kernel.errors.append({"plugin": entry.name, "error": "could not build import spec"})
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            sys.modules.pop(module_name, None)
            kernel.errors.append({
                "plugin": entry.name,
                "error": f"import failed: {e}",
                "traceback": traceback.format_exc(),
            })
            continue

        register = getattr(module, "register", None)
        if not callable(register):
            kernel.errors.append({"plugin": entry.name, "error": "missing register(kernel) function"})
            continue

        kernel._current = record
        try:
            register(kernel)
            kernel.records.append(record)
        except Exception as e:
            kernel.errors.append({
                "plugin": entry.name,
                "error": f"register() failed: {e}",
                "traceback": traceback.format_exc(),
            })
        finally:
            kernel._current = None

    return kernel
