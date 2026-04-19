# GridOS: Agentic Spreadsheet

> **Live SaaS**: [gridos.onrender.com](https://gridos.onrender.com) · **Docs**: [gridos.mintlify.app](https://gridos.mintlify.app) · **Quickstart**: [gridos.mintlify.app/quickstart](https://gridos.mintlify.app/quickstart)

![GridOS demo — build a model by talking to the sheet](./assets/demo.gif)

GridOS pairs a deterministic Python kernel with an LLM to build a spreadsheet you can edit by talking to it. Agents read the current grid state, return structured JSON write-intents, and the kernel previews, collision-checks, and applies them — so the AI can edit the sheet without clobbering locked or occupied cells.

Bring-your-own-key: plug in **Google Gemini**, **Anthropic Claude**, **Groq**, or **OpenRouter** from the in-app settings panel and switch models per-request from the chat composer. Start a fresh workbook by describing what you want to build from the landing page, or open a template — same backend, either entry point.

## Architecture

### `/core` — Deterministic kernel
The source of truth for cell state.
- `engine.py` — coordinate mapping, write collisions, shift logic, lock enforcement, persistence.
- `models.py` — Pydantic schemas for `AgentIntent` and `WriteResponse`.
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
- Exposes REST endpoints for chat, preview/apply, direct cell writes, sheet management, save/load, template library, and per-provider API-key management (`/settings/providers`, `/settings/keys/*`, `/models/available`).

### `/static` — Frontend
Minimal HTML + vanilla JS + Tailwind UI for editing cells, previewing AI suggestions, and managing sheets.

### `/cloud` — Managed (SaaS) tier, optional
Everything here stays dormant unless `SAAS_MODE=true`. The public OSS path imports nothing from this folder into the hot loop — the only always-mounted endpoint is `GET /cloud/status`, which the frontend reads on bootstrap to decide whether to surface login / billing UI. When enabled, the cloud tier adds:

- **Supabase JWT auth** (`cloud/auth.py`) — email/password + Google OAuth, routes ES256/RS256 tokens through JWKS and HS256 through a shared secret.
- **Multi-workbook storage** (`cloud/supabase_store.py`) — each user's workbooks live in `public.workbooks.grid_state` (jsonb), protected by row-level security. A landing-page workbook picker handles list / create / rename / delete.
- **Bring-your-own-key LLMs** (`cloud/user_keys.py`) — each user enters their own Gemini/Anthropic/Groq/OpenRouter key from the in-app Settings panel; rows live in `public.user_api_keys` behind RLS. The operator never pays LLM bills — the product is GridOS itself (cloud save, multi-workbook, agentic UX), not the tokens.
- **Per-user kernel isolation** (`main.py` kernel pool) — a `ContextVar`-bound kernel per `(user_id, workbook_id)`, LRU-capped at 64. Two tabs on different workbooks (or two users on the same process) never step on each other's in-memory state.
- **Per-tier quotas** (`cloud/config.py`) — five subscription tiers with two independent caps. **Monthly agentic tokens** (`free=100k`, `plus=1M`, `student=5M`, `pro=5M`, `enterprise=unlimited`) are the product limit — enforced at `/agent/chat` with a 402 at the cap, even though the user is paying their own LLM bill, so tiers stay meaningful. **Cloud workbook slots** (`free=3`, `plus=10`, `student=25`, `pro=50`, `enterprise=unlimited`) cap per-user storage. The `student` tier is Pro-level on tokens and is intended to be unlocked by `.edu` email / GitHub Student Pack verification (enforcement ships with the Stripe phase).
- **Usage analytics** — every successful LLM response logs to `public.usage_logs`; a Postgres trigger rolls it into `public.user_usage` for the account popover's progress bar.

Run the migrations in `cloud/migrations/` (numbered `0001_init.sql`, `0002_usage_rollup.sql`, …) in the Supabase SQL Editor before pointing a server at your project.

### `/core/workbook_store.py` — Persistence seam
`WorkbookStore` protocol with two implementations: `FileWorkbookStore` (OSS, flat files on disk) and `SupabaseWorkbookStore` (SaaS). Endpoints call `store.save(scope, state_dict)` without branching on mode.

### `/plugins` — Extensibility surface
Drop a directory into `plugins/` with `plugin.py` + `manifest.json` and GridOS auto-loads it on boot. A plugin's `register(kernel)` function can register custom formulas (`@kernel.formula("BLACK_SCHOLES")`), specialist agents (`kernel.agent({...})`), and provider models (`kernel.model({...})`). Example plugins ship in-tree — see [`plugins/hello_world`](./plugins/hello_world), [`plugins/black_scholes`](./plugins/black_scholes), and [`plugins/real_estate`](./plugins/real_estate). Full authoring guide: [`plugins/README.md`](./plugins/README.md). Introspect what loaded (and what failed) at `GET /plugins`.

In SaaS mode an in-app **Marketplace** (gear icon → grid icon in the menubar) lets users browse the vetted plugin catalog and toggle per-user installs, persisted in `public.user_plugins`.

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
| Persistence (SaaS) | Supabase Postgres + RLS (`public.workbooks`, `public.users`, `public.usage_logs`, `public.user_api_keys`) |

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
2. Open **SQL Editor** and run the numbered migrations in `cloud/migrations/` in order: `0001_init.sql` (tables + RLS), `0002_usage_rollup.sql` (usage trigger), `0003_user_api_keys.sql` (BYOK keys table).
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
- [ ] Stripe checkout + webhook for tier upgrades (Phase 4c)
- [ ] `.edu` / GitHub Student Pack verification for the Student tier unlock
- [ ] Range-based vector operations and cross-sheet referencing
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
- Run `python test_platform.py && python test_plugins.py` before sending a PR. Both are offline — no network, no LLM calls.
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
