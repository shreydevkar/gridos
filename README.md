# GridOS: Agentic Spreadsheet

GridOS pairs a deterministic Python kernel with an LLM to build a spreadsheet you can edit by talking to it. Agents read the current grid state, return structured JSON write-intents, and the kernel previews, collision-checks, and applies them — so the AI can edit the sheet without clobbering locked or occupied cells.

Bring-your-own-key: plug in Google Gemini and/or Anthropic Claude from the in-app settings panel and switch models per-request from the chat composer.

## Architecture

### `/core` — Deterministic kernel
The source of truth for cell state.
- `engine.py` — coordinate mapping, write collisions, shift logic, lock enforcement, persistence.
- `models.py` — Pydantic schemas for `AgentIntent` and `WriteResponse`.
- `functions.py` — registry of atomic formula operations (`SUM`, `MAX`, `MIN`, `MINUS`, `MULTIPLY`, `DIVIDE`, `AVERAGE`, `IF`, comparators, …).
- `macros.py` — user-authored macros compiled on top of the primitive registry.
- `utils.py` — A1 notation ↔ (row, col) coordinate translation.

### `/core/providers` — LLM provider abstraction
- `base.py` — `Provider` interface returning a normalized `ProviderResponse`, plus auth/transient error classifiers.
- `catalog.py` — static model catalog (model id → provider, display name, description).
- `gemini.py` / `anthropic.py` — concrete providers wrapping `google-genai` and the `anthropic` SDK.

### `main.py` — Orchestration
A FastAPI app that:
- Streams a live grid snapshot into the LLM prompt.
- Routes prompts to either a finance-specialized or general-purpose agent, and routes the model call to whichever provider owns the selected model id.
- Validates model output against locked ranges before applying.
- Exposes REST endpoints for chat, preview/apply, direct cell writes, sheet management, save/load, template library, and per-provider API-key management (`/settings/providers`, `/settings/keys/*`, `/models/available`).

### `/static` — Frontend
Minimal HTML + vanilla JS + Tailwind UI for editing cells, previewing AI suggestions, and managing sheets.

## Capabilities

- **Formula synthesis** — natural-language prompts become executable grid formulas (e.g. `=MINUS(C3, D3)`).
- **Multi-provider LLMs** — pick between Gemini and Claude models per request from the chat composer; keys live in-app (gear icon) and never need a code change.
- **User macros** — the agent can propose reusable formulas (`=MARGIN(A,B)`) composed from primitives; approved macros are callable from any cell.
- **Chart overlays** — in-app charts render via Chart.js and are upserted by title so the agent can resize/retype them in place.
- **Preset templates** — built-in starters (Simple DCF, Monthly Budget, Break-Even, Loan Amortization, Income Statement) plus user-saved templates, with origin badges to tell them apart.
- **Collision resolution** — shifts data to avoid overwriting occupied or locked cells.
- **Cell locking** — users can mark ranges read-only so the AI can't touch them.
- **State persistence** — workbooks serialize to `.gridos` files; import/export via the File menu.
- **Preview/apply flow** — AI writes go through a preview step before committing.
- **Chain mode** — the agent auto-applies each step, observes formula results, and keeps going until the plan is done.

## Tech stack

| Layer | Tech |
| :--- | :--- |
| Kernel | Python 3.10+ |
| LLM providers | Google Gemini (`google-genai`), Anthropic Claude (`anthropic`) |
| API | FastAPI + Uvicorn |
| Frontend | HTML + vanilla JS + Chart.js |
| Persistence | Custom `.gridos` file format |

## Running locally

Prerequisites: Python 3.10+ and **at least one** LLM API key:

- Google Gemini — get one at [Google AI Studio](https://aistudio.google.com/app/apikey).
- Anthropic Claude — get one at the [Anthropic Console](https://console.anthropic.com/).

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
   ```

Run the server:

```bash
uvicorn main:app --reload
```

Open http://127.0.0.1:8000. The model picker in the chat composer lists every model whose provider has a valid key.

### Supported models

| Model | Provider |
| :--- | :--- |
| `gemini-3.1-flash-lite-preview` | Google Gemini |
| `gemini-3.1-pro` | Google Gemini |
| `claude-haiku-4-5-20251001` | Anthropic |
| `claude-sonnet-4-6` | Anthropic |
| `claude-opus-4-7` | Anthropic |

Add more by editing `core/providers/catalog.py`.

## Roadmap

- [x] Deterministic core — grid memory, locking, collision resolution
- [x] Agentic routing — intent classification, JSON-based writes
- [x] Hybrid interface — reactive UI with AI + manual control
- [x] Multi-step chaining — agent observes formula results and takes follow-up actions
- [x] User-authored macros on top of primitives
- [x] Chart overlays and preset template library
- [x] Multi-LLM support with in-app key management
- [ ] Range-based vector operations and cross-sheet referencing
- [ ] External connectors (stock / weather / etc.)
- [ ] Provider-native structured output (Claude tool-use / OpenAI JSON mode) for stricter JSON reliability

## License

MIT.
