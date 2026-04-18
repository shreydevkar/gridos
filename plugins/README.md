# GridOS Plugins

GridOS loads every subdirectory of this folder as a **plugin** — a small Python module that can register custom formulas, specialist agents, and provider models.

## Writing a plugin

```
plugins/my_plugin/
  manifest.json     # name + description; surfaced in the SaaS marketplace
  plugin.py         # defines register(kernel)
```

`manifest.json`:

```json
{
  "name": "My Plugin",
  "description": "One-line pitch shown on the marketplace card.",
  "category": "finance",
  "author": "Your Name",
  "version": "0.1.0"
}
```

`plugin.py`:

```python
def register(kernel):
    @kernel.formula("BLACK_SCHOLES")
    def black_scholes(S, K, T, r, sigma, option_type="call"):
        ...

    kernel.agent({
        "id": "real_estate",
        "display_name": "Real Estate Copilot",
        "router_description": "cap rate, NOI, cash-on-cash, DSCR",
        "system_prompt": "You are a real-estate underwriting specialist. ..."
    })

    kernel.model({
        "id": "my-org/custom-model-v1",
        "provider": "openrouter",
        "display_name": "My Custom Model",
        "description": "OpenRouter-hosted model shipped with this plugin."
    })
```

That's it. Start the server, and GridOS picks up your plugin on boot.

## The three seams

| Seam | Call | What it does |
| :--- | :--- | :--- |
| Formulas | `@kernel.formula("NAME")` | Registers a callable into the global formula registry. Available from any cell as `=NAME(...)`. Underlying registry lives in [`core/functions.py`](../core/functions.py). |
| Agents | `kernel.agent({...})` | Adds a specialist agent the router can pick. Same shape as [`agents/*.json`](../agents). Required keys: `id`, `system_prompt`. |
| Models | `kernel.model({...})` | Extends [`core/providers/catalog.py`](../core/providers/catalog.py). Required keys: `id`, `provider`, `display_name`, `description`. |

## Developer map — where to look in the core

- **Formulas:** [`core/functions.py`](../core/functions.py) — the formula registry + `FormulaEvaluator`. Your formula becomes a first-class primitive alongside `SUM`, `AVERAGE`, etc.
- **Agents:** [`agents/__init__.py`](../agents/__init__.py) — how built-in agents load. Plugin-registered agents are merged into the same `AGENTS` dict at boot.
- **Model catalog:** [`core/providers/catalog.py`](../core/providers/catalog.py) — the static list the chat composer reads. New entries appear in the picker on next page load (assuming the owning provider has a key configured).
- **Plugin loader:** [`core/plugins.py`](../core/plugins.py) — the `PluginKernel` facade and `discover_and_load()` walker.

## Trust model

Plugins run **in-process** with full Python access — no sandbox, no capability system. That means:

- **OSS / self-hosted**: you own the process, so you own the trust decision. Plugins are auto-loaded on boot.
- **Hosted SaaS** (gridos.onrender.com): plugin loading is gated by `GRIDOS_PLUGINS_ENABLED`; only operator-vetted plugins ship in this directory. The in-app **Marketplace** lets users toggle which vetted plugins apply to their workbook — it's a visibility/discovery layer, not a sandbox.

## Error handling

One bad plugin can't take down the server. If a plugin's `register()` raises, the loader records the error and continues. Check `GET /plugins` at runtime:

```json
{
  "loaded": [
    {"slug": "hello_world", "name": "Hello World", "formulas": ["GREET"], "agents": ["greeter"]}
  ],
  "errors": [
    {"plugin": "broken_example", "error": "register() failed: NameError: ..."}
  ]
}
```

## Example plugins in this repo

- [`hello_world/`](./hello_world) — minimal template: one formula + one agent.
- [`black_scholes/`](./black_scholes) — `=BLACK_SCHOLES(S, K, T, r, sigma, type)` options pricer.
- [`real_estate/`](./real_estate) — domain specialist agent + `=CAP_RATE` / `=DSCR` primitives.

Copy any of them as a starting point.
