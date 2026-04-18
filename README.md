# GridOS: Agentic Spreadsheet

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
Everything here stays dormant unless `SAAS_MODE=true`. The public OSS path imports nothing from this folder into the hot loop — the only always-mounted endpoint is `GET /cloud/status`, which the frontend reads on bootstrap to decide whether to surface login / billing UI. When enabled, `cloud/supabase_store.py` (backed by Supabase Postgres + RLS) replaces the flat-file persistence layer so each user's workbooks live in the cloud. Run `cloud/migrations/0001_init.sql` in the Supabase SQL Editor before pointing a server at your project.

### `/core/workbook_store.py` — Persistence seam
`WorkbookStore` protocol with two implementations: `FileWorkbookStore` (OSS, flat files on disk) and `SupabaseWorkbookStore` (SaaS). Endpoints call `store.save(scope, state_dict)` without branching on mode.

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
- **State persistence** — workbooks serialize to `.gridos` files; import/export via the File menu.
- **Preview/apply flow** — AI writes go through a preview step before committing, with a pre-apply guard that blocks formulas whose inputs are empty.
- **Chain mode** — the agent auto-applies each step, observes formula results, and keeps going until the plan is done.

## Tech stack

| Layer | Tech |
| :--- | :--- |
| Kernel | Python 3.10+ |
| LLM providers | Google Gemini (`google-genai`), Anthropic Claude (`anthropic`), Groq + OpenRouter (`openai` SDK pointed at their OpenAI-compatible endpoints) |
| API | FastAPI + Uvicorn |
| Frontend | HTML + vanilla JS + Chart.js |
| Persistence | Custom `.gridos` file format |

## Running locally

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

Two equivalent options:

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

## GridOS UI Pictures:
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
- [ ] Range-based vector operations and cross-sheet referencing
- [ ] External connectors (stock / weather / etc.)
- [ ] Provider-native structured output (Claude tool-use / OpenAI JSON mode) for stricter JSON reliability
- [ ] Prompt caching on Gemini + Anthropic to cut long-chain latency
- [ ] Embedding-based agent router (needed before ~10 agents)

## License

MIT.
