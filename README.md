# GridOS: Agentic Spreadsheet

[![Tests](https://github.com/shreydevkar/gridos/actions/workflows/tests.yml/badge.svg?branch=master)](https://github.com/shreydevkar/gridos/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Live deploy](https://img.shields.io/badge/live-gridos.onrender.com-brightgreen)](https://gridos.onrender.com)

> **Live SaaS**: [gridos.onrender.com](https://gridos.onrender.com) · **Docs**: [gridos.mintlify.app](https://gridos.mintlify.app) · **Quickstart**: [gridos.mintlify.app/quickstart](https://gridos.mintlify.app/quickstart)

![GridOS demo — build a model by talking to the sheet](./assets/demo.gif)

GridOS pairs a deterministic Python kernel with an LLM to build a spreadsheet you can edit by talking to it. Agents read the current grid state, return structured JSON write-intents, and the kernel previews, collision-checks, and applies them — so the AI can edit the sheet without clobbering locked or occupied cells.

Bring-your-own-key: plug in **Google Gemini**, **Anthropic Claude**, **Groq**, or **OpenRouter** from the in-app settings panel and switch models per-request from the chat composer. Start a fresh workbook by describing what you want to build from the landing page, or open a template — same backend, either entry point.

## Architecture

### `/core` — Deterministic kernel
The source of truth for cell state.
- `engine.py` — coordinate mapping, write collisions, shift logic, lock enforcement, persistence. **Thread-safe via per-kernel `RLock`** so concurrent writers (multi-user collab, agent-apply racing a user edit) can't interleave partial state. **Per-cell version counter** bumps on every commit; `VersionConflict` powers optimistic-locking. **Post-commit hook seam** (`add_post_commit_hook`) lets the orchestration layer broadcast cell changes without polluting the engine with transport concerns. Excel-compatible parser supports comparison ops (`=`, `<>`, `<`, `>`, `<=`, `>=`), string concat (`&`), and preserves text-cell values for non-numeric formulas.
- `models.py` — Pydantic schemas for `AgentIntent`, `WriteResponse`, and `CellState` (incl. `version: int` for optimistic concurrency).
- `functions.py` — registry of atomic formula operations (`SUM`, `MAX`, `MIN`, `MINUS`, `MULTIPLY`, `DIVIDE`, `AVERAGE`, `IF`, comparators, …).
- `macros.py` — user-authored macros compiled on top of the primitive registry.
- `utils.py` — A1 notation ↔ (row, col) coordinate translation.

### `/core/providers` — LLM provider abstraction
- `base.py` — `Provider` interface returning a normalized `ProviderResponse` (text + model + tokens + `finish_reason`), plus auth/transient error classifiers.
- `catalog.py` — static model catalog (model id → provider, display name, description) and fallback-order rules.
- `gemini.py` / `anthropic.py` — concrete providers wrapping `google-genai` and the `anthropic` SDK.
- `groq.py` / `openrouter.py` — OpenAI-compatible providers built on the shared `openai` SDK, pointed at Groq's and OpenRouter's `/v1` endpoints respectively.

### `main.py` — Orchestration
A FastAPI app that:
- Streams a live grid snapshot into the LLM prompt.
- Routes prompts to either a finance-specialized or general-purpose agent, and routes the model call to whichever provider owns the selected model id.
- **Pins the router/classifier call to the fastest configured small model** (GPT-OSS 20B > Llama 8B > Gemini Flash Lite > Claude Haiku) regardless of the user's dropdown choice — trivial task that doesn't need frontier quality; the user's model still drives the agent call.
- Tolerates small-model quirks: balanced-brace extraction for prose-prefixed JSON, clear `422` errors with provider/model/finish_reason context, and a pre-apply formula guard that rejects previews referencing empty cells (blocks `#DIV/0!` before it touches the sheet).
- Validates model output against locked ranges before applying.
- **Server-side preview-token stash** — every `/agent/chat` mints a single-use, TTL-bounded token. `/agent/apply` re-reads the stashed payload server-side and ignores client-supplied values, so the LLM can't substitute different writes between preview and commit. `/agent/write` is refused in SaaS mode (the only sanctioned path is `/agent/chat → /agent/apply`).
- **Resolved-scope ContextVar** (`_current_scope`, `_scope_from_context()`) — collaborator requests resolve to the workbook owner's scope so save/load/rename/delete never silently flip ownership.
- **Realtime broadcaster hook** — registers a post-commit closure on each kernel that POSTs cell deltas to Supabase Realtime in a daemon thread (fire-and-forget, never blocks the request).
- Exposes REST endpoints for chat, preview/apply, direct cell writes, sheet management, save/load, template library, per-provider API-key management, the developer plugin portal, and shared-workbook collaborator CRUD.

### `/static` — Frontend
Minimal HTML + vanilla JS + Chart.js for editing cells, previewing AI suggestions, managing sheets, and rendering live multi-user state.

- **Realtime cell + cursor sync** — subscribes to the Supabase Realtime channel `workbook:<wb_id>` on bootstrap. `cells_changed` events paint remote writes optimistically with a yellow flash + safety-net `fetchGrid` debounce (50ms). `cursor_at` events render Google-Sheets-style range highlights with colored borders, faint inner tint, and a floating email label. Throttled 80ms leading + trailing on the send side; 4s heartbeat re-broadcasts the current selection to recover from silent WebSocket reconnects; 8s TTL sweep removes ghost cursors when peers close their tab.
- **Kill-switch composer** — `AbortController` wired through `/agent/chat` and `/agent/chat/chain`; the send button morphs into a red stop button while a request is in-flight. Click (or press Enter again) to cancel.
- **Debounced cloud auto-save** — every undo-recorded mutation schedules a silent `/system/save` after 4s idle in SaaS mode. Single in-flight guard prevents interleaved saves; status pill flashes "Autosaved → Ready".
- **Edit-collision protection** — when a remote broadcast arrives for a cell the local user is currently editing, the optimistic paint and refresh are deferred until the local edit commits or cancels. No clobbering mid-keystroke.

### `/cloud` — Managed (SaaS) tier, optional
Everything here stays dormant unless `SAAS_MODE=true`. The public OSS path imports nothing from this folder into the hot loop — the only always-mounted endpoint is `GET /cloud/status`, which the frontend reads on bootstrap to decide whether to surface login / billing UI. When enabled, the cloud tier adds:

- **Supabase JWT auth** (`cloud/auth.py`) — email/password + Google OAuth, routes ES256/RS256 tokens through JWKS and HS256 through a shared secret.
- **Multi-workbook storage** (`cloud/supabase_store.py`) — each user's workbooks live in `public.workbooks.grid_state` (jsonb), protected by row-level security. A landing-page workbook picker handles list / create / rename / delete.
- **Bring-your-own-key LLMs** (`cloud/user_keys.py`) — each user enters their own Gemini/Anthropic/Groq/OpenRouter key from the in-app Settings panel; rows live in `public.user_api_keys` behind RLS. The operator never pays LLM bills — the product is GridOS itself (cloud save, multi-workbook, agentic UX), not the tokens.
- **Per-user kernel isolation** (`main.py` kernel pool) — a `ContextVar`-bound kernel per `(owner_id, workbook_id)`, LRU-capped at 64. Two tabs on different workbooks never step on each other's in-memory state. Collaborators on a *shared* workbook resolve to the **owner's** kernel pool entry, so both users see the same live state and the engine's RLock + version counter handle concurrent writes deterministically.
- **Shared workbooks + realtime collab** (`cloud/migrations/0007_workbook_collaborators.sql`, `cloud/migrations/0008_pending_invites.sql`, `cloud/supabase_store.resolve_workbook_access`) — owner invites by email from File → Share…; invitee sees the workbook in a "Shared with me" strip on their landing page. Invites to *unregistered* emails land in `public.pending_invites` and auto-promote the moment the invitee signs up (Postgres trigger). Cell writes broadcast over Supabase Realtime channel `workbook:<wb_id>`; selection broadcasts go on the same channel as `cursor_at` events with start+end so peers see the full selection rectangle (faint colored fill + edge border + email label). v1 is editor-only and refresh-to-see-changes is replaced by sub-second push.
- **Per-user plugin BYOK** (`cloud/migrations/0010_user_plugin_secrets.sql`, `cloud/user_plugin_secrets.py`) — Shopify tokens, Stripe secret keys, GitHub PATs, etc. are stored per-user in `public.user_plugin_secrets` (RLS, owner-only CRUD). Each plugin's `manifest.json` declares the secret slots it needs; the marketplace card surfaces a **Configure** button that renders a password form from the declaration and POSTs to `/settings/plugin-secrets/{slug}`. Values are write-only from the browser — the `GET` endpoint only reports which slots are set, never the value. Collaborators on a shared workbook use the *owner's* secrets, consistent with "owner controls the workspace." OSS mode falls back to env vars so local dev is unchanged.
- **Plugin install gating** (`core/functions._installed_plugins`) — once a user has toggled any plugin in the marketplace, their `user_plugins` selection becomes a per-request ContextVar that gates plugin-sourced formula evaluation. Calling `=GITHUB_STARS(...)` when `github` isn't enabled returns `#NOT_INSTALLED: enable the 'github' plugin in File > Marketplace`. Built-in formulas (SUM, MAX, IF, …) are never gated. New users with zero toggles are treated as "no preferences yet → allow everything" so first-run isn't a wall of refusals.
- **Per-tier quotas** (`cloud/config.py`) — five subscription tiers with two independent caps. **Monthly agentic tokens** (`free=100k`, `plus=1M`, `student=5M`, `pro=5M`, `enterprise=unlimited`) are the product limit — enforced at `/agent/chat` with a 402 at the cap, even though the user is paying their own LLM bill, so tiers stay meaningful. **Cloud workbook slots** (`free=3`, `plus=10`, `student=25`, `pro=50`, `enterprise=unlimited`) cap per-user storage. The `student` tier is Pro-level on tokens and is intended to be unlocked by `.edu` email / GitHub Student Pack verification (enforcement ships with the Stripe phase).
- **Usage analytics** — every successful LLM response logs to `public.usage_logs`; a Postgres trigger rolls it into `public.user_usage` for the account popover's progress bar.

Run the migrations in `cloud/migrations/` (numbered `0001_init.sql` through `0010_user_plugin_secrets.sql`) in the Supabase SQL Editor before pointing a server at your project.

### `/core/workbook_store.py` — Persistence seam
`WorkbookStore` protocol with two implementations: `FileWorkbookStore` (OSS, flat files on disk) and `SupabaseWorkbookStore` (SaaS). Endpoints call `store.save(scope, state_dict)` without branching on mode.

### `/plugins` — Extensibility surface
Drop a directory into `plugins/` with `plugin.py` + `manifest.json` and GridOS auto-loads it on boot. A plugin's `register(kernel)` function can register custom formulas (`@kernel.formula("BLACK_SCHOLES")`), specialist agents (`kernel.agent({...})`), and provider models (`kernel.model({...})`). Each `manifest.json` can declare the per-user secrets its plugin needs (`secrets: [{key, label, placeholder, help, optional?}]`); the marketplace renders a Configure form from that declaration. Plugins read secrets via `kernel.get_secret(slug, key, env_fallback=...)` which resolves per-user values in SaaS and falls back to env vars in OSS.

**Example plugins in-tree:**
- [`plugins/hello_world`](./plugins/hello_world) — minimal template (`=GREET` + greeter agent); the 30-second plugin demo.
- [`plugins/black_scholes`](./plugins/black_scholes) — options pricer (`=BLACK_SCHOLES`).
- [`plugins/real_estate`](./plugins/real_estate) — `=CAP_RATE` + `=DSCR` + a real-estate underwriting specialist agent.
- [`plugins/shopify`](./plugins/shopify) — live store metrics (`=SHOPIFY_REVENUE`, `=SHOPIFY_ORDER_COUNT`, `=SHOPIFY_AVG_ORDER_VALUE`, `=SHOPIFY_PRODUCT_COUNT`). Per-user auth via the marketplace Configure modal, or env vars `SHOPIFY_STORE_DOMAIN` + `SHOPIFY_ADMIN_TOKEN` in OSS.
- [`plugins/stripe`](./plugins/stripe) — live account metrics (`=STRIPE_REVENUE`, `=STRIPE_CHARGE_COUNT`, `=STRIPE_MRR`, `=STRIPE_ACTIVE_SUBSCRIBERS`, `=STRIPE_CUSTOMER_COUNT`). MRR normalizes day/week/month/year intervals into monthly. Auth via `STRIPE_SECRET_KEY` (per-user or env).
- [`plugins/github`](./plugins/github) — public repo stats (`=GITHUB_STARS`, `=GITHUB_FORKS`, `=GITHUB_OPEN_ISSUES`, `=GITHUB_COMMITS_LAST_N_DAYS`). Works zero-auth within GitHub's 60 req/hr anon limit; optional `GITHUB_TOKEN` bumps to 5000/hr and unlocks private repos.

Full authoring guide: [`plugins/README.md`](./plugins/README.md). Introspect what loaded (and what failed) at `GET /plugins`.

In SaaS mode an in-app **Marketplace** (gear icon → grid icon in the menubar) lets users browse the vetted plugin catalog, search + filter by category / install-status, install or uninstall per-user (persisted in `public.user_plugins`), and **Configure** per-plugin credentials (persisted in `public.user_plugin_secrets`). Plugin-sourced formulas are gated per-user once any toggle has been made, so calling `=STRIPE_MRR()` without Stripe installed returns a clear `#NOT_INSTALLED` sentinel.

**Developer plugin portal** (OSS only, gated by `GRIDOS_DEV_PORTAL_ENABLED=1`) — File → View → "Developer plugin portal…" opens a modal with the loaded-plugin list, a slug + plugin.py upload form, and an inline formula tester that runs against an ephemeral kernel so the live workbook stays clean. `POST /dev/plugins/upload` writes the files and hot-registers; `DELETE /dev/plugins/{slug}` unregisters and removes; `POST /dev/plugins/test` evaluates a formula in isolation. Refused unconditionally in SaaS — uploading Python = full RCE on the server, so the marketplace is the sanctioned distribution path there.

**Self-evolving formula loop** — when a user asks for a formula that isn't expressible as a macro (needs HTTP, a SaaS API, custom Python), the agent can emit a `plugin_spec` field with `{slug, name, description, plugin_py, example_formula}`. The preview card renders the proposed code in a syntax-highlighted block with an **Install plugin** button that POSTs to the dev portal. Code is never exec'd without explicit user approval, button disables after install to block double-upload.

## Capabilities

- **Formula synthesis** — natural-language prompts become executable grid formulas (e.g. `=MINUS(C3, D3)`).
- **Multi-provider LLMs** — pick between Gemini, Claude, Groq, and OpenRouter models per request from the chat composer; keys live in-app (gear icon) and never need a code change.
- **Landing-page hero prompt** — describe what you want to build ("Build a 4-quarter revenue forecast with 10% QoQ growth") and GridOS clears the kernel, routes you to the workbook, and auto-submits the prompt so you land on a sheet that's already building.
- **Persistent reasoning history** — agent preview cards freeze in the chat thread after Apply/Dismiss with colored outcome badges (`APPLIED`, `DISMISSED`, `SUPERSEDED`), so the full audit trail of *what the agent was thinking* stays visible. The thread is part of workbook state: it survives page reloads and rides along inside the `.gridos` file when you export and re-import, so the conversation stays coupled to the sheet it produced.
- **User macros** — the agent can propose reusable formulas (`=MARGIN(A,B)`) composed from primitives; approved macros are callable from any cell.
- **Chart overlays** — in-app charts render via Chart.js and are upserted by title so the agent can resize/retype them in place.
- **Preset templates** — built-in starters (Simple DCF, Monthly Budget, Break-Even, Loan Amortization, Income Statement) plus user-saved templates, with origin badges to tell them apart.
- **Collision resolution** — shifts data to avoid overwriting occupied or locked cells.
- **Cell locking** — users can mark ranges read-only so the AI can't touch them.
- **State persistence** — workbooks serialize to `.gridos` files; import/export via the File menu. In SaaS mode, save also writes to Supabase so the workbook (and its chat thread) roam across browsers.
- **`.xlsx` round-trip** — download any workbook as `.xlsx` (openpyxl) and drag into Google Sheets, or import an Excel file back into GridOS to replace the current workbook's contents.
- **Chat shortcuts** — typing `clear all` / `delete all` in the chat bypasses the LLM entirely and runs the clear-sheet command directly, so common housekeeping phrases don't burn tokens or hit provider rate limits.
- **Preview/apply flow** — AI writes go through a preview step before committing, with a pre-apply guard that blocks formulas whose inputs are empty.
- **Chain mode** — the agent auto-applies each step, observes formula results, and keeps going until the plan is done.
- **Multi-section builds in one call** — for structured deliverables (3-statement model, full operating model, DCF, multi-block dashboard), the agent emits an `intents` array packing every rectangle into a single response. One LLM call, one Apply click, ~6× fewer tokens than walking the same model through chain mode.
- **String literals in formulas** — formulas accept quoted strings (`=GREET("Shrey")`, `=BLACK_SCHOLES(100, 100, 1, 0.05, 0.2, "call")`), enabling plugins that take labels or enum-style switches without needing cell references.
- **Per-cell decimal precision** — two toolbar buttons (`.0←` / `.00→`) round the displayed number without touching the stored value, so downstream formulas still see full precision.
- **Excel-compatible formula parser** — `=A1=B1`, `=IF(A1<>0, x, y)`, `=A1<=B1`, `=A1&" world"`, `=A1<B1+C1` all work. Comparison operators return 1/0 (Excel-style), `&` coerces both sides to display strings (booleans → `TRUE`/`FALSE`, integer-valued floats drop the `.0`), text-cell references stay as strings so numeric ops raise `#VALUE!` honestly instead of silently coercing to 0.
- **Cross-sheet formula references** — `=Data!A1`, `=SUM(Data!A1:A10)`, `='Monthly Budget'!B5` (quoted names for sheets with spaces). Sheet-name match is case-insensitive; missing sheet yields `#REF!`. The agent knows the grammar: ask "pull A1 from Sheet 2 into B2" and it emits `=Sheet2!A1` without prompting. The preview guardrail correctly skips cross-sheet refs instead of false-positive blocking them as "empty cells on the current sheet."
- **Shared workbooks (SaaS)** — File → Share… invites a collaborator by email; both users edit the same live kernel with sub-second sync. **Realtime cell updates** paint with a yellow flash on the peer tab; **range cursors** show the other user's selection rectangle Google-Sheets-style with a colored border + faint inner tint + email label. Concurrent writes are serialized by a per-kernel `RLock` and per-cell version counter so two users can't corrupt each other's state.
- **Optimistic-locking API** — `/grid/cell` and `/grid/range` accept an `expected_versions: {cell: int}` map and return **409 Conflict** when the stored version drifted. Lets future "merge or refresh" UX detect concurrent writes precisely instead of falling back to last-writer-wins.
- **Composer kill-switch** — the send button morphs into a red stop button while an `/agent/chat` or `/agent/chat/chain` request is in-flight. Click (or press Enter again) to abort. Status pill flips to "Cancelled"; chain cancels refetch the grid so server-committed steps stay honest.
- **Debounced cloud auto-save** — every undo-recorded mutation triggers a silent `/system/save` after 4s idle (SaaS only). No manual Ctrl+S required for cloud users; status pill flashes "Autosaved → Ready".
- **Self-evolving formula loop** — for a formula that needs HTTP, an external API, or non-trivial Python, the agent proposes a full plugin (slug + plugin.py + example usage). The preview card shows the code; one click installs it via the developer portal and the new formula becomes immediately callable from any cell.
- **Hardened guardrail** — every preview from `/agent/chat` mints a server-side single-use token; `/agent/apply` re-reads the stashed payload and ignores client-supplied values, so the LLM can't substitute different writes between preview and commit. The Python kernel is the only sanctioned path to mutate cells.
- **Live connectors (Shopify / Stripe / GitHub)** — three shipped plugins that turn spreadsheet cells into live dashboards against third-party APIs. `=STRIPE_MRR()`, `=SHOPIFY_REVENUE(30)`, `=GITHUB_STARS("vercel/next.js")` — BYOK per-user via the marketplace Configure modal, 60s in-process cache, honest `#*_AUTH!` / `#*_OFFLINE!` / `#*_RATE_LIMIT!` sentinels on failure.
- **Per-user plugin BYOK + install gating** — the marketplace Configure button opens a password-input form rendered from each plugin's declared secret slots (`manifest.json.secrets`). Values land in `public.user_plugin_secrets` (RLS, never shipped back down to the browser). Once a user toggles any plugin install, uninstalled plugin formulas return a clean `#NOT_INSTALLED` sentinel instead of silently working.
- **Invite-by-email for unregistered users** — Share… modal accepts any email, not just existing GridOS users. Unregistered invites sit in `pending_invites` until the invitee signs up; a Postgres trigger atomically promotes them into `workbook_collaborators` on account creation, so the shared workbook is in their "Shared with me" strip on first visit.
- **Marketplace search + filters** — live search over plugin name/slug/description/author/formula-names, plus category and install-status filters. Auto-populates the category dropdown from the installed plugins' manifests. Per-plugin branded logos (Shopify / Stripe / GitHub have official marks; every other plugin gets a monogram fallback keyed off the slug).

## How is GridOS different from X?

The short version: GridOS is the only one of these that's **open-source, self-hostable, and designed as a kernel you can extend with plugins**. Everything else is a closed SaaS.

| | GridOS | Excel + Copilot | Rows.com | Equals.app | Causal |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Open source** | ✅ MIT | ❌ | ❌ | ❌ | ❌ |
| **Self-hostable** | ✅ `uvicorn main:app` | ❌ | ❌ | ❌ | ❌ |
| **Bring-your-own LLM key** | ✅ Gemini / Claude / Groq / OpenRouter | ❌ (Microsoft-hosted) | ❌ | ❌ | ❌ |
| **Pluggable formulas / agents / models** | ✅ Drop a dir into `plugins/` | ❌ | Limited integrations | Limited integrations | ❌ |
| **AI writes are preview-first, not blind** | ✅ Collision-checked + locked-cell-aware | Partial | Partial | Partial | N/A |
| **Multi-section builds in one call** | ✅ `intents` array packs a whole model | ❌ step-by-step | ❌ | ❌ | ❌ |
| **Free tier without credit card** | ✅ (self-host or BYOK) | Microsoft 365 required | Free tier exists | Paid | Free tier exists |
| **Primary audience** | Developers + power users who want to extend | Microsoft 365 users | Data teams | Finance teams | Startup finance / planning |

**When GridOS wins:** you want to extend the spreadsheet itself (custom formulas, domain-specific agents, connect your own models), self-host, or use LLM providers that aren't OpenAI.

**When the others win:** you're already in Microsoft 365 and want AI inside the exact spreadsheet your team is already using (Copilot); you're building dashboards with live integrations to Stripe/HubSpot/Postgres without code (Rows); you're a finance team that wants a Google-Sheets-like collaborative SaaS with database-backed cells (Equals); you're building startup financial models with variables and scenarios (Causal).

GridOS doesn't try to be the best finance planning tool or the best dashboard tool. It tries to be **the best spreadsheet-shaped surface for developers to build on top of**, and a usable AI spreadsheet as a side-effect.

## Documentation

Full docs live at **[gridos.mintlify.app](https://gridos.mintlify.app)**. Jumping-off points:

**Getting started**
- [Introduction](https://gridos.mintlify.app/introduction) — what GridOS is, at a glance
- [Quickstart](https://gridos.mintlify.app/quickstart) — run it locally and build your first workbook
- [Add API keys](https://gridos.mintlify.app/api-keys) — connect Gemini / Claude / Groq / OpenRouter

**Core concepts**
- [Workbooks, sheets, and cells](https://gridos.mintlify.app/concepts/workbook)
- [Chat and agents](https://gridos.mintlify.app/concepts/chat-and-agents) — how the agent reads and edits the sheet
- [Preview & apply](https://gridos.mintlify.app/concepts/preview-apply) — review AI edits before they land
- [Formulas](https://gridos.mintlify.app/concepts/formulas) — built-in functions and syntax

**Configuration**
- [LLM providers](https://gridos.mintlify.app/configuration/llm-providers)
- [Supported models](https://gridos.mintlify.app/configuration/supported-models)
- [Cell locking](https://gridos.mintlify.app/configuration/cell-locking)

**Feature guides**
- [Chain mode](https://gridos.mintlify.app/guides/chain-mode) — let the AI build the workbook end-to-end
- [Templates](https://gridos.mintlify.app/guides/templates)
- [Charts](https://gridos.mintlify.app/guides/charts)
- [Macros](https://gridos.mintlify.app/guides/macros)
- [Building financial models](https://gridos.mintlify.app/guides/building-financial-models)

**REST API reference**
- [`POST /agent/chat`](https://gridos.mintlify.app/api/agent-chat) · [`POST /agent/apply`](https://gridos.mintlify.app/api/agent-apply) · [`POST /agent/chat/chain`](https://gridos.mintlify.app/api/agent-chain)
- [Grid cell writes](https://gridos.mintlify.app/api/grid-cell) · [Workbook / sheets](https://gridos.mintlify.app/api/workbook-sheets) · [Save / load / export](https://gridos.mintlify.app/api/save-load-export)
- [Charts](https://gridos.mintlify.app/api/charts) · [Provider settings](https://gridos.mintlify.app/api/settings-providers)

**Troubleshooting**
- [Common errors](https://gridos.mintlify.app/troubleshooting/common-errors) — keys, guards, and load failures
- [Model output issues](https://gridos.mintlify.app/troubleshooting/model-output-issues)

## Tech stack

| Layer | Tech |
| :--- | :--- |
| Kernel | Python 3.10+ |
| LLM providers | Google Gemini (`google-genai`), Anthropic Claude (`anthropic`), Groq + OpenRouter (`openai` SDK pointed at their OpenAI-compatible endpoints) |
| API | FastAPI + Uvicorn |
| Frontend | HTML + vanilla JS + Chart.js |
| Persistence (OSS) | Custom `.gridos` file format (+ `.xlsx` round-trip via `openpyxl`) |
| Persistence (SaaS) | Supabase Postgres + RLS (`public.workbooks`, `public.users`, `public.usage_logs`, `public.user_api_keys`, `public.user_plugins`, `public.workbook_collaborators`, `public.pending_invites`, `public.user_plugin_secrets`) |
| Realtime (SaaS) | Supabase Realtime broadcast — `workbook:<wb_id>` channel carries `cells_changed` + `cursor_at` events; server posts via REST, client subscribes via supabase-js |

## Running locally

> Prefer a walkthrough? The [Quickstart](https://gridos.mintlify.app/quickstart) in the docs covers this section step-by-step with screenshots.

Prerequisites: Python 3.10+ and **at least one** LLM API key. Any one of the four will work:

- **Google Gemini** — free tier at [Google AI Studio](https://aistudio.google.com/app/apikey).
- **Anthropic Claude** — $5 in starter credits at the [Anthropic Console](https://console.anthropic.com/).
- **Groq** — genuinely free (no credit card required), very fast; sign up at [console.groq.com](https://console.groq.com). **Recommended dev driver.**
- **OpenRouter** — free models with rate limits at [openrouter.ai](https://openrouter.ai); good fallback, occasionally flaky.

```bash
git clone https://github.com/shreydevkar/gridos.git
cd gridos

python -m venv .venv
source .venv/bin/activate   # Windows PowerShell: .venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

### Providing API keys

Full per-provider instructions live in [Add API keys](https://gridos.mintlify.app/api-keys). The short version — two equivalent options:

1. **In-app settings (recommended)** — run the server, click the gear icon in the menubar, paste a key for each provider you want to use. Keys are stored in `data/api_keys.json`, which is gitignored.

2. **`.env` file** — create `.env` in the repo root (works as a backstop even when no key is saved in-app):

   ```
   GOOGLE_API_KEY=your_gemini_key
   ANTHROPIC_API_KEY=your_claude_key
   GROQ_API_KEY=your_groq_key
   OPENROUTER_API_KEY=your_openrouter_key
   ```

Run the server:

```bash
uvicorn main:app --reload
```

Open http://127.0.0.1:8000. The model picker in the chat composer lists every model whose provider has a valid key.

### Supported models

| Model | Provider | Notes |
| :--- | :--- | :--- |
| `gemini-3.1-flash-lite-preview` | Google Gemini | Fast, generous free tier — good daily driver. |
| `gemini-3.1-pro` | Google Gemini | Higher quality, slower. |
| `claude-haiku-4-5-20251001` | Anthropic | Cheap + fast. |
| `claude-sonnet-4-6` | Anthropic | Balanced. |
| `claude-opus-4-7` | Anthropic | Best quality, slowest. |
| `openai/gpt-oss-120b` | Groq | ~500 tps; strongest free model for strict JSON output. |
| `openai/gpt-oss-20b` | Groq | ~1000 tps; fastest option, used for the router call. |
| `qwen/qwen3-32b` | Groq | Preview; strong at structured output. |
| `llama-3.3-70b-versatile` | Groq | Capable; occasionally prefaces JSON with prose. |
| `llama-3.1-8b-instant` | Groq | Tiny + instant; great for classifiers. |
| `nousresearch/hermes-3-llama-3.1-405b:free` | OpenRouter | Free 405B reasoning model. |
| `meta-llama/llama-3.3-70b-instruct:free` | OpenRouter | Free Llama 70B. |
| `meta-llama/llama-3.2-3b-instruct:free` | OpenRouter | Free and tiny. |
| `openrouter/free` | OpenRouter | Meta-router — picks a working free model automatically. |

Add more by editing `core/providers/catalog.py`. The UI picks them up on next page load as long as the owning provider has a configured key.

## Running as a hosted SaaS

> A live reference deployment of this exact config is at **[gridos.onrender.com](https://gridos.onrender.com)** — free tier, auto-deployed from `master`.

The cloud tier is optional — set `SAAS_MODE=true` and point the server at a Supabase project, and every request is auth-gated, multi-tenant, and quota-tracked.

### One-time Supabase setup

1. Create a Supabase project.
2. Open **SQL Editor** and run the numbered migrations in `cloud/migrations/` in order: `0001_init.sql` (tables + RLS), `0002_usage_rollup.sql` (usage trigger), `0003_user_api_keys.sql` (LLM BYOK), `0004_add_plus_tier.sql` + `0005_add_student_tier.sql` (tier check constraints), `0006_user_plugins.sql` (per-user plugin enablement), `0007_workbook_collaborators.sql` (shared-workbook ACL + extended `workbooks` RLS), `0008_pending_invites.sql` (invite-by-email for unregistered users + auto-promote trigger), `0009_fix_pending_invites_unique.sql` (constraint fix for 0008's upsert), `0010_user_plugin_secrets.sql` (per-user plugin BYOK keys).
3. **Authentication → Providers** — enable Email and (optionally) Google. Google needs a Google Cloud Console OAuth 2.0 Client with `https://<project>.supabase.co/auth/v1/callback` as an authorized redirect URI.
4. **Project Settings → API** — copy the `URL`, `anon public` key, `service_role` key, and `JWT Secret`.

### Required env

```
SAAS_MODE=true
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_ANON_KEY=<anon public key>         # browser-safe, RLS enforced
SUPABASE_SERVICE_ROLE_KEY=<service role key> # SERVER ONLY, bypasses RLS
SUPABASE_JWT_SECRET=<JWT secret>             # server-side token verification
```

**LLM keys are BYOK** — each signed-in user adds their own Gemini/Anthropic/Groq/OpenRouter key from the in-app Settings panel; the server never uses operator-side LLM credentials in SaaS mode. Any `GOOGLE_API_KEY` / `GROQ_API_KEY` env vars are ignored when `SAAS_MODE=true`.

Each tier still has a monthly **agentic-token budget** that caps how many tokens the product will run on the user's key (see `cloud/config.py`). This is the SaaS paywall, not an operator-cost control — the user pays the LLM bill either way, but upgrading unlocks a bigger budget of agentic automation.

Optional tuning (defaults shown — `enterprise` is always unlimited on both axes):

```
FREE_TIER_MONTHLY_TOKENS=100000       # monthly agentic-token budget; 0 = unlimited
PLUS_TIER_MONTHLY_TOKENS=1000000
STUDENT_TIER_MONTHLY_TOKENS=5000000
PRO_TIER_MONTHLY_TOKENS=5000000
FREE_TIER_MAX_WORKBOOKS=3             # cloud storage slots per user; 0 = unlimited
PLUS_TIER_MAX_WORKBOOKS=10
STUDENT_TIER_MAX_WORKBOOKS=25
PRO_TIER_MAX_WORKBOOKS=50
```

### Deploying to Render (free tier)

Render's free web service is a good fit — the FastAPI backend serves the static frontend directly, so no separate static host is needed.

1. Push the repo to GitHub.
2. Create a Render **Web Service** pointed at the repo.
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - Health check path: `/healthz`
3. Paste every env var from above into Render's dashboard (never commit them).
4. Deploy. Render assigns a `*.onrender.com` URL; add it to Supabase **Authentication → URL Configuration → Site URL** so OAuth redirects resolve.

Render's free instances sleep after ~15 min of inactivity (~30–60s cold start on first hit). Point a free UptimeRobot monitor at `/healthz` every 5 minutes to keep the dyno warm during the day.

## GridOS OSS UI:
<img width="1920" height="1095" alt="Screenshot 2026-04-17 171916" src="https://github.com/user-attachments/assets/2b69ef11-69b0-4fce-8415-b29166e3dbd3" />

<img width="1898" height="1085" alt="Screenshot 2026-04-17 172353" src="https://github.com/user-attachments/assets/0a8c4972-da67-433d-bc99-fa19f8fd3aed" />

## Roadmap

- [x] Deterministic core — grid memory, locking, collision resolution
- [x] Agentic routing — intent classification, JSON-based writes
- [x] Hybrid interface — reactive UI with AI + manual control
- [x] Multi-step chaining — agent observes formula results and takes follow-up actions
- [x] User-authored macros on top of primitives
- [x] Chart overlays and preset template library
- [x] Multi-LLM support with in-app key management (Gemini, Claude, Groq, OpenRouter)
- [x] Landing-page hero prompt → auto-submitting workbook start
- [x] Persistent reasoning history with Apply/Dismiss outcome badges
- [x] Chat thread persists with the workbook — survives reload + `.gridos` export/import
- [x] Pre-apply formula-dependency guard (blocks `#DIV/0!` before it hits the sheet)
- [x] Router call pinned to fastest small model for ~40% wall-clock speedup
- [x] `.xlsx` round-trip (openpyxl) for Excel + Google Sheets interop
- [x] Optional SaaS tier: Supabase auth, multi-workbook cloud storage, per-tier token + slot quotas, usage analytics
- [x] Per-user kernel isolation — `ContextVar`-bound kernel per `(user_id, workbook_id)`, LRU-capped at 64
- [x] BYOK — per-user LLM keys stored server-side in `public.user_api_keys` (RLS), set from the in-app Settings panel
- [x] Render deploy — `/healthz`, `render.yaml`, same-origin `API_BASE` (live at [gridos.onrender.com](https://gridos.onrender.com))
- [x] Five-tier pricing ladder (Free / Plus / Student / Pro / Enterprise) with independent token + slot caps
- [x] V0 plugin architecture — auto-loader for custom formulas, specialist agents, and provider models (see [`plugins/`](./plugins))
- [x] In-app plugin marketplace (SaaS) — browse + install per-user, persisted in `public.user_plugins`, per-surface type badges (Formula / Agent / Model)
- [x] String literals in the formula parser — plugins can take quoted args like `=BLACK_SCHOLES(..., "call")`
- [x] Per-cell decimal display precision — toolbar buttons adjust rounding without touching stored values
- [x] Multi-rectangle agent responses (`intents` array) — whole multi-section models build in one LLM call instead of N chain turns
- [x] Excel-compatible parser ops — comparison (`=`, `<>`, `<`, `>`, `<=`, `>=`), string concat (`&`), and text-cell preservation in references
- [x] Cross-sheet formula references — `=SheetName!A1` / `='Quoted Name'!A1:A10`, wired into the agent's base system prompt so the AI emits them unprompted; preview guardrail is cross-sheet-aware
- [x] Hardened guardrail — server-minted single-use preview tokens stop client-side payload substitution between preview and apply
- [x] Composer kill-switch — `AbortController` cancels any in-flight `/agent/chat[/chain]` mid-call
- [x] Debounced cloud auto-save — silent `/system/save` 4s after the last edit, SaaS only
- [x] Concurrency primitives — per-kernel `RLock` + per-cell version counter + optimistic-locking 409 on `/grid/cell`
- [x] Shared workbooks (v1, editor-only) — collaborators resolve to the owner's live kernel; invite/list/revoke endpoints + File → Share… modal + "Shared with me" landing-page strip
- [x] Realtime collab — Supabase Realtime broadcast for `cells_changed` + `cursor_at` (full range selection); 80ms throttle, 4s heartbeat, 8s TTL sweep
- [x] Self-evolving formula loop — agent emits `plugin_spec`; preview card installs via dev portal on user approval
- [x] Developer plugin portal — File → View → "Developer plugin portal…" hot-uploads + tests + deletes plugins; gated by `GRIDOS_DEV_PORTAL_ENABLED` and refused in SaaS
- [x] Shopify connector plugin — `=SHOPIFY_REVENUE` / `_ORDER_COUNT` / `_AVG_ORDER_VALUE` / `_PRODUCT_COUNT` with 60s cache and `#SHOPIFY_*!` sentinels on auth/network failure
- [x] Stripe connector plugin — `=STRIPE_REVENUE` / `_CHARGE_COUNT` / `_MRR` / `_ACTIVE_SUBSCRIBERS` / `_CUSTOMER_COUNT` with MRR normalization across billing intervals
- [x] GitHub connector plugin — `=GITHUB_STARS` / `_FORKS` / `_OPEN_ISSUES` / `_COMMITS_LAST_N_DAYS` for any public repo, optional `GITHUB_TOKEN` for higher rate limit
- [x] Per-user plugin BYOK — each plugin declares required secrets in its manifest; the marketplace Configure modal stores them per-user in `public.user_plugin_secrets` (RLS-protected, write-only from the browser)
- [x] Plugin install gating — `_installed_plugins` ContextVar enforces the marketplace toggle at formula-evaluation time; uninstalled plugin formulas return `#NOT_INSTALLED`
- [x] Invite-by-email for unregistered users — `pending_invites` table + Postgres auto-promote trigger on user signup
- [x] Marketplace polish — live search, category/status filters, per-plugin branded logos, Configure button for credential setup
- [x] Delete-key broadcasts — `clear_cells` / `clear_unlocked` fire the realtime post-commit hook so collaborators see deletions without a refresh
- [ ] Viewer role enforcement — schema column ready; need to wire `_require_editor()` into every mutation endpoint and surface the role choice in the Share UI
- [ ] Per-user connector credential UI for viewers — share the owner's keys but let viewers override with their own account when that makes sense
- [ ] Stripe checkout + webhook for tier upgrades (Phase 4c)
- [ ] `.edu` / GitHub Student Pack verification for the Student tier unlock
- [ ] Cross-sheet dirty tracking — v1 reads the other sheet correctly on first compute; upstream writes don't auto-propagate yet
- [ ] Range-based vector operations
- [ ] External connectors (stock / weather / etc.)
- [ ] Provider-native structured output (Claude tool-use / OpenAI JSON mode) for stricter JSON reliability
- [ ] Prompt caching on Gemini + Anthropic to cut long-chain latency
- [ ] Embedding-based agent router (needed before ~10 agents)

## Contributing

GridOS is open-core, and there are two ways to get involved.

### 1. Core contributors

Working on the kernel itself — new primitives, provider adapters, collision-engine improvements, SaaS features. Start here:

- Fork the repo and follow [Running locally](#running-locally) to get the server up.
- Read the [Architecture](#architecture) section above for a map of `core/`, `main.py`, `cloud/`, and the static frontend.
- Run `python test_platform.py && python test_ast_edge_cases.py && python test_plugins.py` before sending a PR. All three are offline — no network, no LLM calls. `test_ast_edge_cases.py` covers 30 parser cases (operator precedence, comparison ops, string concat, range refs, IF branches, cross-sheet references + quoted sheet names, circular-ref termination, deterministic failure on unknown functions).
- PRs welcome for anything on the [Roadmap](#roadmap) or anything you think the project is missing. Open an issue first for larger architectural changes.

### 2. Plugin and extension developers

Shipping standalone formulas, agents, or models without touching the core. This is the lower-friction path and is where most third-party work belongs. GridOS's plugin system is designed to make your contribution usable **immediately** after someone drops your directory into `plugins/` — no re-architecture required.

**60-second plugin:**

```python
# plugins/my_pack/plugin.py
def register(kernel):
    @kernel.formula("BLACK_SCHOLES")
    def black_scholes(S, K, T, r, sigma, option_type="call"):
        ...

    kernel.agent({
        "id": "real_estate",
        "display_name": "Real Estate Copilot",
        "router_description": "cap rate, NOI, DSCR, pro-formas",
        "system_prompt": "You are a real-estate underwriting specialist. ..."
    })
```

Then `plugins/my_pack/manifest.json` with name/description/category so the marketplace can surface it. Full guide, examples, and the developer map: **[`plugins/README.md`](./plugins/README.md)**.

**Developer map — where to look in the core:**

| You want to add… | Look at | Seam |
| :--- | :--- | :--- |
| A custom formula (`=BLACK_SCHOLES`, `=GET_BTC_PRICE`) | [`core/functions.py`](./core/functions.py) | `@kernel.formula("NAME")` |
| A specialist agent (real-estate copilot, ML-ops agent) | [`agents/__init__.py`](./agents/__init__.py) + [`agents/*.json`](./agents/) | `kernel.agent({...})` |
| A new LLM provider or model | [`core/providers/catalog.py`](./core/providers/catalog.py) | `kernel.model({...})` |
| State / persistence changes | [`core/workbook_store.py`](./core/workbook_store.py) | core-contributor PR (not plugin-addressable yet) |

## License

MIT.
