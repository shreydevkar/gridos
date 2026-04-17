# GridOS: Agentic Spreadsheet

GridOS pairs a deterministic Python kernel with an LLM (Google Gemini) to build a spreadsheet you can edit by talking to it. Agents read the current grid state, return structured JSON write-intents, and the kernel previews, collision-checks, and applies them — so the AI can edit the sheet without clobbering locked or occupied cells.

## Architecture

### `/core` — Deterministic kernel
The source of truth for cell state.
- `engine.py` — coordinate mapping, write collisions, shift logic, lock enforcement, persistence.
- `models.py` — Pydantic schemas for `AgentIntent` and `WriteResponse`.
- `functions.py` — registry of atomic formula operations (`SUM`, `MAX`, `MIN`, `MINUS`).
- `utils.py` — A1 notation ↔ (row, col) coordinate translation.

### `main.py` — Orchestration
A FastAPI app that:
- Streams a live grid snapshot into the LLM prompt.
- Routes prompts to either a finance-specialized or general-purpose agent.
- Validates model output against locked ranges before applying.
- Exposes REST endpoints for chat, preview/apply, direct cell writes, sheet management, and save/load.

### `/static` — Frontend
Minimal HTML + vanilla JS + Tailwind UI for editing cells, previewing AI suggestions, and managing sheets.

## Capabilities

- **Formula synthesis** — natural-language prompts become executable grid formulas (e.g. `=MINUS(C3, D3)`).
- **Collision resolution** — shifts data to avoid overwriting occupied or locked cells.
- **Cell locking** — users can mark ranges read-only so the AI can't touch them.
- **State persistence** — workbooks serialize to `.gridos` files.
- **Preview/apply flow** — AI writes go through a preview step before committing.

## Tech stack

| Layer | Tech |
| :--- | :--- |
| Kernel | Python 3.10+ |
| LLM | Google Gemini (via `google-generativeai`) |
| API | FastAPI + Uvicorn |
| Frontend | HTML + Tailwind + vanilla JS |
| Persistence | Custom `.gridos` file format |

## Running locally

Prerequisites: Python 3.10+ and a Google Gemini API key from [Google AI Studio](https://aistudio.google.com/app/apikey).

```bash
git clone https://github.com/shreydevkar/gridos_kernel.git
cd gridos_kernel

python -m venv .venv
source .venv/bin/activate   # Windows PowerShell: .venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

Create a `.env` file in the repo root (see `.env.example`):

```
GOOGLE_API_KEY=your_key_here
```

Then run the server:

```bash
uvicorn main:app --reload
```

Open http://127.0.0.1:8000.

## Roadmap

- [x] Deterministic core — grid memory, locking, collision resolution
- [x] Agentic routing — intent classification, JSON-based writes
- [x] Hybrid interface — reactive UI with AI + manual control
- [ ] Multi-step chaining — agent observes formula results and takes follow-up actions
- [ ] Range-based vector operations and cross-sheet referencing
- [ ] External connectors (stock / weather / etc.)

## License

MIT.
