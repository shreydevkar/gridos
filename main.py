import json
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types
from pydantic import BaseModel

from agents import load_agents
from core.engine import GridOSKernel
from core.functions import FormulaEvaluator
from core.macros import MacroError, compile_macro
from core.models import AgentIntent, WriteResponse
from core.utils import a1_to_coords


load_dotenv()
client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
MODEL_NAME = "gemini-3.1-flash-lite-preview"
TELEMETRY_PATH = Path("telemetry_log.json")
MAX_CHAIN_ITERATIONS = 10

DATA_DIR = Path("data")
TEMPLATES_DIR = DATA_DIR / "templates"
MACROS_PATH = DATA_DIR / "user_macros.json"
HERO_TOOLS_PATH = DATA_DIR / "hero_tools.json"

HERO_TOOLS_CATALOG = [
    {
        "id": "web_search",
        "display_name": "Web Search",
        "description": "(placeholder) Advises the agent that live web lookups are available. Actual fetching is not wired up.",
    },
    {
        "id": "live_data",
        "display_name": "Live Data Puller",
        "description": "(placeholder) Advises the agent that external API/data feeds are available. Actual fetching is not wired up.",
    },
]

app = FastAPI(title="GridOS - Agentic Workbook")
kernel = GridOSKernel()
AGENTS = load_agents()

USER_MACROS: list[dict] = []
HERO_TOOLS_STATE: dict[str, bool] = {t["id"]: False for t in HERO_TOOLS_CATALOG}

os.makedirs("static", exist_ok=True)
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------- Library persistence ----------


def _builtin_primitive_names() -> list[str]:
    return sorted(kernel.evaluator.registry.keys())


def _macro_names() -> set[str]:
    return {m["name"].upper() for m in USER_MACROS}


def _register_macro(spec: dict) -> None:
    """Compile a macro spec and insert the resulting callable into the evaluator."""
    # Exclude any previously-registered version of this macro from the primitive pool
    # so macros can be updated safely and so a macro can't (accidentally) recurse into itself.
    macro_name = spec["name"].upper()
    primitive_registry = {
        k: v for k, v in kernel.evaluator.registry.items() if k.upper() != macro_name
    }
    fn = compile_macro(
        name=spec["name"],
        params=spec.get("params", []),
        body=spec["body"],
        registry=primitive_registry,
    )
    kernel.evaluator.register_custom(macro_name, fn)


def _load_user_macros() -> None:
    if not MACROS_PATH.exists():
        return
    try:
        raw = json.loads(MACROS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    if not isinstance(raw, list):
        return
    for spec in raw:
        try:
            _register_macro(spec)
        except MacroError:
            # Drop invalid stored macros silently; they're user-authored and may
            # predate schema changes. They'll reappear on the next successful save.
            continue
        USER_MACROS.append({
            "name": spec["name"].upper(),
            "description": spec.get("description", ""),
            "params": [p.upper() for p in spec.get("params", [])],
            "body": spec["body"],
        })


def _persist_user_macros() -> None:
    MACROS_PATH.write_text(json.dumps(USER_MACROS, indent=2), encoding="utf-8")


def _load_hero_tools() -> None:
    if not HERO_TOOLS_PATH.exists():
        return
    try:
        raw = json.loads(HERO_TOOLS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    if not isinstance(raw, dict):
        return
    for tool in HERO_TOOLS_CATALOG:
        HERO_TOOLS_STATE[tool["id"]] = bool(raw.get(tool["id"], False))


def _persist_hero_tools() -> None:
    HERO_TOOLS_PATH.write_text(json.dumps(HERO_TOOLS_STATE, indent=2), encoding="utf-8")


_load_user_macros()
_load_hero_tools()


# ---------- Telemetry ----------


def _append_telemetry(entry: dict) -> None:
    existing: list = []
    if TELEMETRY_PATH.exists():
        try:
            existing = json.loads(TELEMETRY_PATH.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        except json.JSONDecodeError:
            existing = []
    existing.append(entry)
    TELEMETRY_PATH.write_text(json.dumps(existing, indent=2), encoding="utf-8")


_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


def _is_transient_model_error(exc: Exception) -> bool:
    """Detect Gemini errors that are worth retrying (overload, rate limit, gateway)."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(code, int) and code in _TRANSIENT_STATUS_CODES:
        return True
    msg = str(exc).lower()
    return any(s in msg for s in ("503", "429", "unavailable", "overloaded", "rate limit", "resource exhausted"))


def call_model(agent_id: str, *, contents, config=None, max_attempts: int = 4):
    """Wrap generate_content with telemetry + exponential-backoff retry on transient errors."""
    kwargs: dict = {"model": MODEL_NAME, "contents": contents}
    if config is not None:
        kwargs["config"] = config

    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.models.generate_content(**kwargs)
            break
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts or not _is_transient_model_error(exc):
                raise
            # Exponential backoff with jitter: ~1s, ~2s, ~4s
            delay = (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            time.sleep(delay)
    else:
        # unreachable — the for/else runs only if the loop completes without break
        if last_exc:
            raise last_exc
        raise RuntimeError("call_model exhausted retries with no response")

    usage = getattr(response, "usage_metadata", None)
    _append_telemetry({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent_id": agent_id,
        "model": MODEL_NAME,
        "prompt_token_count": getattr(usage, "prompt_token_count", None) if usage else None,
        "candidates_token_count": getattr(usage, "candidates_token_count", None) if usage else None,
        "total_token_count": getattr(usage, "total_token_count", None) if usage else None,
    })
    return response


# ---------- Request models ----------


class ChatRequest(BaseModel):
    prompt: str
    history: List[Dict[str, str]] = []
    scope: str = "sheet"
    selected_cells: List[str] = []
    sheet: Optional[str] = None


class ChainRequest(ChatRequest):
    max_iterations: int = MAX_CHAIN_ITERATIONS


class FormulaRequest(BaseModel):
    function_name: str
    arguments: list[float]


class CellUpdateRequest(BaseModel):
    cell: str
    value: Optional[str] = ""
    sheet: Optional[str] = None


class RangeUpdateRequest(BaseModel):
    target_cell: str
    values: list[list[str | int | float | bool | None]]
    sheet: Optional[str] = None


class PreviewApplyRequest(BaseModel):
    sheet: Optional[str] = None
    agent_id: str
    target_cell: str
    values: list[list]
    shift_direction: str = "right"
    chart_spec: Optional[Dict[str, Any]] = None


class SheetCreateRequest(BaseModel):
    name: Optional[str] = None


class SheetRenameRequest(BaseModel):
    old_name: str
    new_name: str


class SheetActivateRequest(BaseModel):
    name: str


# ---------- Agent routing & prompts ----------


BASE_SYSTEM_RULES = (
    "You are operating within GridOS. Check the \"locked\" metadata for every cell "
    "before proposing a write. Do not attempt to overwrite locked cells."
)

OUTPUT_FORMAT_SPEC = """
OUTPUT FORMAT: strictly valid JSON (no markdown fences):
{
    "reasoning": "Short explanation.",
    "target_cell": "A1",
    "values": [["..."]],
    "chart_spec": null,
    "macro_spec": null,
    "plan": null
}

If the user is asking for a MULTI-SECTION build (financial model, forecast, 3-statement, operating model, DCF, budget, etc.), emit a `plan` object on the FIRST turn only. It is informational — the system shows it to the user so they can see the full structure before cells are filled:
{
    "title": "Quarterly Operating Model",
    "anchor": "B2",
    "sections": [
        {"label": "Header row",          "target": "B2:F2", "notes": "Metric, Q1..Q4"},
        {"label": "Revenue",             "target": "B3:F3", "notes": "10% QoQ growth from 100"},
        {"label": "COGS",                "target": "B4:F4", "notes": "=MULTIPLY(revenue, 0.4)"}
    ]
}
On this first turn, `values` contains ONLY the first section. On subsequent chain turns, omit `plan` (set null) and write the NEXT section. Emit values=[[""]] when every section is done.

If the user asks for a chart/graph/visualization, also fill in chart_spec:
{
    "data_range": "A1:B6",        // rectangular range covering labels + values
    "chart_type": "bar",          // one of: bar, line, pie
    "title": "Scores",
    "anchor_cell": "D2",          // top-left cell where the chart overlay appears; pick an empty area
    "orientation": "columns"      // "columns" = first column is labels (typical); "rows" = first row is labels
}
Omit chart_spec (leave as null) when the user is only writing data or editing cells.

If the user wants to MOVE, RESIZE, RETYPE, or RENAME an EXISTING chart (e.g. "place the performance chart at F16", "make the scores chart a pie"), reuse the SAME title as the existing chart in chart_spec — the system will update it in place. In that case set "values": null and set "target_cell" to the new anchor_cell. Do NOT invent placeholder data.

If the user asks for a NEW named formula/metric that is not already listed in USER MACROS or the built-in primitives, you MAY propose a new macro by filling in macro_spec:
{
    "name": "MARGIN",                    // unique identifier, letters/digits/underscore only
    "params": ["A", "B"],                // parameter names (uppercase letters)
    "description": "Gross margin: (A - B) / A",
    "body": "=DIVIDE(MINUS(A, B), A)"   // MUST only call registered primitives. Nested primitive calls ARE allowed here (this is the one place nesting is permitted — macro bodies are composed expressions). No infix operators, no references to other user macros.
}
Proposed macros are NOT saved automatically — the user reviews and approves them. In the SAME response, do NOT write any cell values that call the proposed macro (it isn't registered yet). Keep "values" null or write unrelated cells. The user will re-ask after approval to use the new macro.
""".strip()


def route_prompt(prompt: str, history_context: str) -> str:
    if len(AGENTS) == 1:
        return next(iter(AGENTS))

    options = "\n".join(
        f"- {agent['id']}: {agent.get('router_description', agent.get('display_name', agent['id']))}"
        for agent in AGENTS.values()
    )
    instruction = f"""
Analyze this user task: "{prompt}".
Previous context: {history_context}

Available agent profiles:
{options}

Return ONLY the lowercase agent id that best fits the task. No other text.
""".strip()

    res = call_model("router", contents=instruction)
    candidate = res.text.strip().lower().split()[0] if res.text else "general"
    return candidate if candidate in AGENTS else "general"


def build_system_instruction(agent: dict, context: dict, req: ChatRequest) -> str:
    selected_summary = ", ".join(req.selected_cells) if req.selected_cells else "No cells selected."
    scope_line = "Selected cells only" if req.scope == "selection" else "Entire active sheet"
    bounds = context.get("occupied_bounds")
    bounds_line = (
        f"Occupied region: {bounds['top_left']} -> {bounds['bottom_right']} "
        f"({bounds['rows']} rows x {bounds['cols']} cols)"
        if bounds else "Occupied region: empty"
    )

    existing_charts = kernel.list_charts(req.sheet or kernel.active_sheet)
    if existing_charts:
        chart_lines = [
            f"- \"{c.get('title') or '(untitled)'}\" ({c.get('chart_type')}, range {c.get('data_range')}, anchor {c.get('anchor_cell')})"
            for c in existing_charts
        ]
        charts_section = "EXISTING CHARTS ON THIS SHEET (reuse the same title in chart_spec to update one):\n" + "\n".join(chart_lines)
    else:
        charts_section = "EXISTING CHARTS ON THIS SHEET: none"

    if USER_MACROS:
        macro_lines = [
            f"- {m['name']}({', '.join(m['params'])}) — {m.get('description') or 'user macro'}"
            for m in USER_MACROS
        ]
        macros_section = (
            "USER MACROS (callable like built-in formulas, single flat call, no nesting in the grid):\n"
            + "\n".join(macro_lines)
        )
    else:
        macros_section = "USER MACROS: none"

    enabled_hero_ids = [t["id"] for t in HERO_TOOLS_CATALOG if HERO_TOOLS_STATE.get(t["id"])]
    if enabled_hero_ids:
        hero_lines = [
            f"- {t['display_name']}: {t['description']}"
            for t in HERO_TOOLS_CATALOG
            if t["id"] in enabled_hero_ids
        ]
        hero_section = (
            "HERO TOOLS ENABLED (advisory — they are not wired up yet, mention capability only if the user asks):\n"
            + "\n".join(hero_lines)
        )
    else:
        hero_section = "HERO TOOLS ENABLED: none"

    primitive_names = _builtin_primitive_names()
    primitives_section = (
        "AVAILABLE PRIMITIVES (authoritative — if a function is not in this list, it does NOT exist; "
        "do not invent names like POWER, LN, IF, etc. unless they appear here):\n"
        + ", ".join(primitive_names)
    )

    sections = [
        BASE_SYSTEM_RULES,
        f"ACTIVE SHEET: {req.sheet or kernel.active_sheet}\nVIEW SCOPE: {scope_line}\nSELECTED CELLS: {selected_summary}\n{bounds_line}",
        f"CELL METADATA (a1 -> {{val, locked, type}}):\n{context['cell_metadata_json']}",
        f"READABLE GRID STATE:\n{context['formatted_data']}",
        charts_section,
        primitives_section,
        macros_section,
        hero_section,
    ]

    if req.history:
        history_lines = "\n".join(f"{h['role'].upper()}: {h['content']}" for h in req.history)
        sections.append(
            "CONVERSATION HISTORY (oldest first — the first user message is the original task; "
            "check it for any targets you have not yet written):\n" + history_lines
        )

    sections.extend([agent["system_prompt"], OUTPUT_FORMAT_SPEC])
    return "\n\n".join(sections)


def _parse_ai_response(text: str) -> dict:
    cleaned = text.replace("```json", "").replace("```", "").strip()
    return json.loads(cleaned)


def _sanitize_plan(raw: Any) -> Optional[dict]:
    """Coerce an agent-emitted plan into a shape the UI can render safely. Returns None if empty."""
    if not isinstance(raw, dict):
        return None
    sections_raw = raw.get("sections")
    if not isinstance(sections_raw, list) or not sections_raw:
        return None
    sections = []
    for item in sections_raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        target = str(item.get("target") or "").strip()
        notes = str(item.get("notes") or "").strip()
        if not label and not target and not notes:
            continue
        sections.append({"label": label, "target": target, "notes": notes})
    if not sections:
        return None
    return {
        "title": str(raw.get("title") or "").strip(),
        "anchor": str(raw.get("anchor") or "").strip(),
        "sections": sections,
    }


def _validate_proposed_macro(raw: Any) -> tuple[Optional[dict], Optional[str]]:
    """Dry-compile an agent-proposed macro. Returns (normalized_spec, error_message)."""
    if not raw or not isinstance(raw, dict):
        return None, None
    name = str(raw.get("name") or "").strip()
    body = str(raw.get("body") or "").strip()
    if not name or not body:
        return None, "Macro proposal missing name or body."
    params_raw = raw.get("params") or []
    if not isinstance(params_raw, list):
        return None, "Macro params must be a list."
    params = [str(p).strip() for p in params_raw if str(p).strip()]

    # Exclude the same name from the registry ONLY if it's currently a user macro
    # (so proposing an update to an existing macro is allowed). Built-in primitives
    # must still collision-check, because macros can't shadow them.
    upper = name.upper()
    existing_macro_names = _macro_names()
    if upper in existing_macro_names:
        registry = {k: v for k, v in kernel.evaluator.registry.items() if k.upper() != upper}
    else:
        registry = dict(kernel.evaluator.registry)
    try:
        compile_macro(name=name, params=params, body=body, registry=registry)
    except MacroError as e:
        return None, str(e)

    return {
        "name": upper,
        "params": [p.upper() for p in params],
        "description": str(raw.get("description") or "").strip(),
        "body": body,
        "replaces_existing": upper in _macro_names(),
    }, None


def generate_agent_preview(req: ChatRequest) -> dict:
    sheet = req.sheet or kernel.active_sheet
    context = kernel.get_context_for_ai(sheet, req.selected_cells, req.scope)
    history_context = "\n".join([f"{h['role'].upper()}: {h['content']}" for h in req.history])

    agent_id = route_prompt(req.prompt, history_context)
    agent = AGENTS[agent_id]
    system_instruction = build_system_instruction(agent, context, req)

    final_response = call_model(
        agent_id,
        contents=req.prompt,
        config=types.GenerateContentConfig(system_instruction=system_instruction),
    )
    ai_data = _parse_ai_response(final_response.text or "")

    raw_values = ai_data.get("values")
    raw_target = ai_data.get("target_cell")
    chart_spec = ai_data.get("chart_spec")
    proposed_macro, macro_error = _validate_proposed_macro(ai_data.get("macro_spec"))
    plan = _sanitize_plan(ai_data.get("plan"))
    fallback_target = req.selected_cells[0] if req.selected_cells else "A1"

    has_values = isinstance(raw_values, list) and any(
        any(v not in ("", None) for v in row) for row in raw_values if isinstance(row, list)
    )

    if not has_values and (chart_spec or proposed_macro):
        # No-cells operation: just a chart update and/or a macro proposal.
        return {
            "category": agent_id,
            "reasoning": ai_data.get("reasoning"),
            "sheet": sheet,
            "scope": req.scope,
            "selected_cells": req.selected_cells,
            "agent_id": agent_id,
            "target_cell": raw_target or fallback_target,
            "original_request": raw_target or fallback_target,
            "preview_cells": [],
            "values": None,
            "chart_spec": chart_spec,
            "proposed_macro": proposed_macro,
            "macro_error": macro_error,
            "plan": plan,
        }

    intent = AgentIntent(
        agent_id=agent_id,
        target_start_a1=raw_target or fallback_target,
        data_payload=raw_values if isinstance(raw_values, list) and raw_values else [["Error"]],
        shift_direction="right",
    )

    preview = kernel.preview_agent_intent(intent, sheet)
    return {
        "category": agent_id,
        "reasoning": ai_data.get("reasoning"),
        "sheet": sheet,
        "scope": req.scope,
        "selected_cells": req.selected_cells,
        "agent_id": agent_id,
        "target_cell": preview["actual_target"],
        "original_request": preview["original_target"],
        "preview_cells": preview["preview_cells"],
        "values": raw_values,
        "chart_spec": chart_spec,
        "proposed_macro": proposed_macro,
        "macro_error": macro_error,
        "plan": plan,
    }


# ---------- Endpoints ----------


@app.get("/")
async def serve_landing():
    return FileResponse("static/landing.html")


@app.get("/workbook")
async def serve_workbook():
    return FileResponse("static/index.html")


@app.get("/agents")
async def list_agents():
    return {
        "agents": [
            {"id": a["id"], "display_name": a.get("display_name", a["id"]), "router_description": a.get("router_description", "")}
            for a in AGENTS.values()
        ]
    }


@app.post("/agent/chat")
async def chat_with_agent(req: ChatRequest):
    try:
        return generate_agent_preview(req)
    except Exception as e:
        if _is_transient_model_error(e):
            raise HTTPException(
                status_code=503,
                detail="Gemini is temporarily overloaded (tried 4x with backoff). Wait a moment and try again.",
            )
        raise HTTPException(status_code=500, detail=f"Agent Error: {str(e)}")


@app.post("/agent/apply")
async def apply_agent_preview(req: PreviewApplyRequest):
    intent = AgentIntent(
        agent_id=req.agent_id,
        target_start_a1=req.target_cell,
        data_payload=req.values,
        shift_direction=req.shift_direction,
    )
    requested, actual = kernel.process_agent_intent(intent, req.sheet)
    chart = None
    if req.chart_spec:
        try:
            chart = kernel.add_chart(req.chart_spec, sheet_name=req.sheet)
        except Exception as e:
            return {
                "status": "Partial",
                "sheet": req.sheet or kernel.active_sheet,
                "actual_target": actual,
                "chart_error": f"Chart skipped: {e}",
            }
    return {
        "status": "Success" if requested == actual else "Collision Resolved",
        "sheet": req.sheet or kernel.active_sheet,
        "actual_target": actual,
        "chart": chart,
    }


_CELL_REF_RE = re.compile(r"[A-Z]+\d+")


def _formula_references_text_cell(formula: str, sheet_state: dict) -> list[str]:
    """Scan cell references inside a formula; return the A1 names of any that
    resolve to a non-numeric value (typical off-by-one symptom: formula pointing
    at a row-label column). Empty list means all refs look numeric."""
    bad_refs: list[str] = []
    for ref in _CELL_REF_RE.findall(formula.upper()):
        try:
            r, c = a1_to_coords(ref)
        except ValueError:
            continue
        cell = sheet_state["cells"].get((r, c))
        if cell is None:
            continue  # empty cell is fine (coerced to 0, intentional)
        v = cell.value
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            continue
        if v in ("", None):
            continue
        bad_refs.append(ref)
    return bad_refs


def _observe_written_cells(preview_cells: list[dict], sheet: str) -> list[dict]:
    """After applying, collect computed values for each just-written cell. Attach a
    'warning' field when the formula dereferenced a text-valued cell — the classic
    off-by-one-column mistake the agent makes on labeled rows."""
    state = kernel._sheet_state(sheet)
    observations = []
    for item in preview_cells:
        a1 = item["cell"]
        try:
            r, c = a1_to_coords(a1)
        except ValueError:
            continue
        cell = state["cells"].get((r, c))
        if cell is None:
            continue
        warning = None
        if cell.formula:
            bad_refs = _formula_references_text_cell(cell.formula, state)
            if bad_refs:
                labels = ", ".join(f"{ref}={state['cells'][a1_to_coords(ref)].value!r}" for ref in bad_refs)
                warning = (
                    f"Formula in {a1} references non-numeric cell(s): {labels}. "
                    f"This is the COLUMN ALIGNMENT bug — the formula in column "
                    f"{a1.rstrip('0123456789')} must reference cells in column "
                    f"{a1.rstrip('0123456789')}, not a label column."
                )
        observations.append({
            "cell": a1,
            "value": cell.value,
            "formula": cell.formula,
            "warning": warning,
        })
    return observations


def _is_completion_signal(values) -> bool:
    """An agent signals 'task complete' by returning an empty-string grid."""
    if not values:
        return True
    for row in values:
        for v in row:
            if v not in ("", None):
                return False
    return True


@app.post("/agent/chat/chain")
async def chat_chain(req: ChainRequest):
    """Auto-apply the agent's writes, observe formula results, and loop up to max_iterations times."""
    try:
        sheet = req.sheet or kernel.active_sheet
        history = list(req.history)
        steps: list[dict] = []
        current_prompt = req.prompt
        max_iters = max(1, min(req.max_iterations, MAX_CHAIN_ITERATIONS))

        # Plan-level state persists across iterations: the agent declares its plan
        # once on turn 1, then subsequent turns omit it. We need to remember it so
        # the chain keeps running until every section is written (or max_iters).
        active_plan: Optional[dict] = None

        for iteration in range(max_iters):
            chat_req = ChatRequest(
                prompt=current_prompt,
                history=history,
                scope=req.scope,
                selected_cells=req.selected_cells,
                sheet=sheet,
            )
            preview = generate_agent_preview(chat_req)
            values = preview["values"] or [[""]]
            chart_spec = preview.get("chart_spec")
            proposed_macro = preview.get("proposed_macro")
            macro_error = preview.get("macro_error")
            plan = preview.get("plan")
            if plan and active_plan is None:
                active_plan = plan
            skip_cell_write = preview["values"] is None and (chart_spec is not None or proposed_macro is not None)

            if _is_completion_signal(values) and not skip_cell_write:
                steps.append({
                    "iteration": iteration,
                    "agent_id": preview["agent_id"],
                    "reasoning": preview["reasoning"],
                    "target": preview["original_request"],
                    "values": values,
                    "observations": [],
                    "completion_signal": True,
                    "proposed_macro": proposed_macro,
                    "macro_error": macro_error,
                    "plan": plan,
                })
                break

            if skip_cell_write:
                actual_target = preview["original_request"]
                observations = []
                formula_observations = []
            else:
                intent = AgentIntent(
                    agent_id=preview["agent_id"],
                    target_start_a1=preview["original_request"],
                    data_payload=values,
                    shift_direction="right",
                )
                _, actual_target = kernel.process_agent_intent(intent, sheet)
                observations = _observe_written_cells(preview["preview_cells"], sheet)
                formula_observations = [o for o in observations if o["formula"]]

            chart = None
            chart_error = None
            if chart_spec:
                try:
                    chart = kernel.add_chart(chart_spec, sheet_name=sheet)
                except Exception as e:
                    chart_error = str(e)

            steps.append({
                "iteration": iteration,
                "agent_id": preview["agent_id"],
                "reasoning": preview["reasoning"],
                "target": actual_target,
                "values": values,
                "observations": observations,
                "completion_signal": False,
                "chart": chart,
                "chart_error": chart_error,
                "proposed_macro": proposed_macro,
                "macro_error": macro_error,
                "plan": plan,
            })

            assistant_payload = {
                "reasoning": preview["reasoning"],
                "target": actual_target,
                "values": values,
            }
            if plan:
                assistant_payload["plan"] = plan

            history.append({"role": "user", "content": current_prompt})
            history.append({"role": "assistant", "content": json.dumps(assistant_payload)})

            # Continue the chain while an active plan still has sections left, or while we're
            # seeing formulas (legacy follow-up heuristic). Break only when neither applies.
            # Only count a section as "written" if the step had no column-alignment warnings —
            # otherwise the agent needs to retry the SAME section, not advance.
            non_completion_steps = [s for s in steps if not s.get("completion_signal")]
            sections_written = sum(
                1
                for s in non_completion_steps
                if not any(o.get("warning") for o in s.get("observations", []))
            )
            plan_sections_total = len(active_plan.get("sections", [])) if active_plan else 0
            plan_remaining = plan_sections_total - sections_written
            has_plan_work = plan_remaining > 0
            has_retry_work = any(o.get("warning") for o in observations)
            if not formula_observations and not has_plan_work and not has_retry_work:
                break

            obs_part = ""
            if formula_observations:
                summary = ", ".join(f"{o['cell']}={o['value']}" for o in formula_observations)
                obs_part = f"Observed after last write: [{summary}]. "

            warning_obs = [o for o in observations if o.get("warning")]
            warning_part = ""
            if warning_obs:
                bullets = "\n".join(f"- {o['warning']}" for o in warning_obs)
                warning_part = (
                    "\n\n*** COLUMN ALIGNMENT WARNINGS — YOU MUST FIX THESE BEFORE MOVING ON ***\n"
                    f"{bullets}\n"
                    "Re-emit the SAME section (same target cell) with corrected formulas. "
                    "Each formula in column X must reference cells in column X, not a label column. "
                    "Do NOT move to the next section until the current section has no warnings.\n\n"
                )

            plan_part = ""
            if has_plan_work:
                next_section = active_plan["sections"][sections_written] if sections_written < plan_sections_total else None
                next_hint = ""
                if next_section:
                    bits = []
                    if next_section.get("label"):
                        bits.append(f"label \"{next_section['label']}\"")
                    if next_section.get("target"):
                        bits.append(f"target range {next_section['target']}")
                    if next_section.get("notes"):
                        bits.append(f"notes: {next_section['notes']}")
                    if bits:
                        label = "The section to (re-)write is" if has_retry_work else "The next section is"
                        next_hint = f" {label}: " + "; ".join(bits) + "."
                verb = "re-emit" if has_retry_work else "write"
                plan_part = (
                    f"You declared a {plan_sections_total}-section plan on turn 1. "
                    f"{sections_written} section(s) have been written cleanly so far. "
                    f"{plan_remaining} section(s) remain (or need retry).{next_hint} "
                    f"{verb.capitalize()} ONLY that one section now. "
                    "Do NOT re-emit the plan (set plan=null). "
                    "When every section is done, signal completion by returning values=[[\"\"]]."
                )

            current_prompt = (
                warning_part
                + obs_part
                + plan_part
                + " If the ORIGINAL task has more targets left to write, produce the next one now and do NOT "
                "repeat cells you have already written. If every part of the original task is done, signal "
                "completion by returning values=[[\"\"]] with target_cell equal to the last written cell."
            )

        return {
            "sheet": sheet,
            "steps": steps,
            "iterations_used": len(steps),
            "terminated_early": len(steps) < max_iters,
        }
    except Exception as e:
        if _is_transient_model_error(e):
            raise HTTPException(
                status_code=503,
                detail="Gemini is temporarily overloaded (tried 4x with backoff). Wait a moment and try again.",
            )
        raise HTTPException(status_code=500, detail=f"Chain Error: {str(e)}")


@app.post("/agent/write", response_model=WriteResponse)
async def agent_write(intent: AgentIntent):
    try:
        requested_a1, actual_a1 = kernel.process_agent_intent(intent)
        return WriteResponse(
            status="Success" if requested_a1 == actual_a1 else "Collision Resolved",
            original_target=requested_a1,
            actual_target=actual_a1,
            message=f"Wrote data starting at {actual_a1}",
        )
    except Exception as e:
        return WriteResponse(
            status="Error",
            original_target=intent.target_start_a1,
            actual_target=intent.target_start_a1,
            message=str(e),
        )


@app.get("/debug/grid")
async def get_grid(sheet: Optional[str] = None):
    target = sheet or kernel.active_sheet
    return {
        "sheet": target,
        "cells": kernel.export_sheet(target),
        "charts": kernel.list_charts(target),
    }


@app.get("/api/workbook")
async def get_workbook():
    return {
        "active_sheet": kernel.active_sheet,
        "sheets": kernel.list_sheets(),
    }


@app.post("/workbook/sheet")
async def create_sheet(req: SheetCreateRequest):
    name = kernel.create_sheet(req.name)
    return {"sheet": name, "sheets": kernel.list_sheets(), "active_sheet": kernel.active_sheet}


@app.post("/workbook/sheet/rename")
async def rename_sheet(req: SheetRenameRequest):
    name = kernel.rename_sheet(req.old_name, req.new_name)
    return {"sheet": name, "sheets": kernel.list_sheets(), "active_sheet": kernel.active_sheet}


@app.post("/workbook/sheet/activate")
async def activate_sheet(req: SheetActivateRequest):
    name = kernel.activate_sheet(req.name)
    return {"sheet": name, "sheets": kernel.list_sheets(), "active_sheet": kernel.active_sheet}


@app.post("/grid/cell")
async def update_cell(req: CellUpdateRequest):
    try:
        target = kernel.write_user_cell(req.cell.upper(), req.value, user_id="User", sheet_name=req.sheet)
        return {"status": "Success", "cell": target, "sheet": req.sheet or kernel.active_sheet}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/grid/range")
async def update_range(req: RangeUpdateRequest):
    try:
        target = kernel.write_user_range(req.target_cell.upper(), req.values, user_id="User", sheet_name=req.sheet)
        return {"status": "Success", "target": target, "sheet": req.sheet or kernel.active_sheet}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/system/save")
async def save_grid():
    kernel.save_state()
    return {"status": "Success"}


@app.post("/system/load")
async def load_grid():
    if kernel.load_state():
        return {"status": "Success"}
    return {"status": "Error", "message": "No save file found."}


@app.get("/system/export")
async def export_workbook():
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"gridos-workbook-{timestamp}.gridos"
    body = json.dumps(kernel.export_state_dict(), indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/system/import")
async def import_workbook(payload: dict = Body(...)):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid workbook payload.")
    try:
        kernel.apply_state_dict(payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not import workbook: {e}")
    return {"status": "Success"}


@app.post("/formula/evaluate")
async def evaluate_formula(req: FormulaRequest):
    evaluator = FormulaEvaluator()
    return {"result": evaluator.evaluate(req.function_name, req.arguments)}


@app.post("/system/clear")
async def clear_grid(sheet: Optional[str] = None):
    kernel.clear_unlocked(sheet)
    return {"status": "Success", "sheet": sheet or kernel.active_sheet}


@app.post("/system/unlock-all")
async def unlock_all():
    """Forcibly clear the locked flag on every cell across every sheet, and drop
    any empty-locked placeholder cells that have no value/formula."""
    dropped = 0
    unlocked = 0
    for sheet_name, state in kernel.sheets.items():
        cells = state["cells"]
        for coords in list(cells.keys()):
            cell = cells[coords]
            if not cell.locked:
                continue
            if cell.value in (None, "") and not cell.formula:
                del cells[coords]
                dropped += 1
            else:
                cell.locked = False
                unlocked += 1
    return {"status": "Success", "unlocked": unlocked, "dropped": dropped}


# ---------- Charts ----------


class ChartCreateRequest(BaseModel):
    anchor_cell: str = "F2"
    data_range: str
    chart_type: str = "bar"
    title: str = ""
    width: int = 400
    height: int = 280
    orientation: str = "columns"
    sheet: Optional[str] = None


class ChartUpdateRequest(BaseModel):
    anchor_cell: Optional[str] = None
    data_range: Optional[str] = None
    chart_type: Optional[str] = None
    title: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    orientation: Optional[str] = None
    sheet: Optional[str] = None


@app.get("/system/charts")
async def list_charts(sheet: Optional[str] = None):
    return {"charts": kernel.list_charts(sheet)}


@app.post("/system/charts")
async def create_chart(req: ChartCreateRequest):
    spec = req.model_dump(exclude={"sheet"})
    try:
        chart = kernel.add_chart(spec, sheet_name=req.sheet)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not create chart: {e}")
    return {"status": "Success", "chart": chart}


@app.patch("/system/charts/{chart_id}")
async def update_chart(chart_id: str, req: ChartUpdateRequest):
    updates = req.model_dump(exclude={"sheet"}, exclude_none=True)
    try:
        chart = kernel.update_chart(chart_id, updates, sheet_name=req.sheet)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not update chart: {e}")
    return {"status": "Success", "chart": chart}


@app.delete("/system/charts/{chart_id}")
async def delete_chart(chart_id: str, sheet: Optional[str] = None):
    if not kernel.delete_chart(chart_id, sheet_name=sheet):
        raise HTTPException(status_code=404, detail=f"Chart '{chart_id}' not found.")
    return {"status": "Success"}


# ---------- Library: templates ----------


_TEMPLATE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_SAFE_SLUG_RE = re.compile(r"[^A-Za-z0-9_\-]+")


def _slugify_template_name(name: str) -> str:
    slug = _SAFE_SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or "template"


def _template_path(template_id: str) -> Path:
    if not _TEMPLATE_ID_RE.match(template_id):
        raise HTTPException(status_code=400, detail="Invalid template id.")
    return TEMPLATES_DIR / f"{template_id}.json"


class TemplateSaveRequest(BaseModel):
    name: str
    description: Optional[str] = ""


class MacroSaveRequest(BaseModel):
    name: str
    description: Optional[str] = ""
    params: List[str] = []
    body: str


class HeroToolToggleRequest(BaseModel):
    tool_id: str
    enabled: bool


@app.post("/templates/save")
async def save_template(req: TemplateSaveRequest):
    if not req.name or not req.name.strip():
        raise HTTPException(status_code=400, detail="Template name is required.")

    base_slug = _slugify_template_name(req.name)
    candidate = base_slug
    counter = 2
    while (TEMPLATES_DIR / f"{candidate}.json").exists():
        candidate = f"{base_slug}-{counter}"
        counter += 1

    created_at = datetime.now(timezone.utc).isoformat()
    snapshot = {
        "id": candidate,
        "name": req.name.strip(),
        "description": (req.description or "").strip(),
        "created_at": created_at,
        "state": kernel.export_state_dict(),
    }
    (TEMPLATES_DIR / f"{candidate}.json").write_text(
        json.dumps(snapshot, indent=2), encoding="utf-8"
    )
    return {"status": "Success", "template": _template_summary(snapshot)}


def _template_summary(payload: dict) -> dict:
    state = payload.get("state") or {}
    sheets = state.get("sheets") or {}
    cell_count = 0
    for sheet in sheets.values():
        cell_count += len((sheet or {}).get("cells") or {})
    return {
        "id": payload.get("id"),
        "name": payload.get("name"),
        "description": payload.get("description", ""),
        "created_at": payload.get("created_at"),
        "sheet_count": len(sheets),
        "cell_count": cell_count,
    }


@app.get("/templates/list")
async def list_templates():
    templates: list[dict] = []
    for path in sorted(TEMPLATES_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        templates.append(_template_summary(payload))
    templates.sort(key=lambda t: t.get("created_at") or "", reverse=True)
    return {"templates": templates}


@app.get("/templates/load/{template_id}")
async def load_template(template_id: str):
    path = _template_path(template_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found.")
    return json.loads(path.read_text(encoding="utf-8"))


@app.post("/templates/apply/{template_id}")
async def apply_template(template_id: str):
    path = _template_path(template_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found.")
    payload = json.loads(path.read_text(encoding="utf-8"))
    try:
        result = kernel.apply_template_respecting_locks(payload.get("state") or {})
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not apply template: {e}")
    return {"status": "Success", **result}


@app.delete("/templates/{template_id}")
async def delete_template(template_id: str):
    path = _template_path(template_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found.")
    path.unlink()
    return {"status": "Success"}


# ---------- Library: tools ----------


@app.get("/tools/list")
async def list_tools():
    return {
        "primitives": [
            {"name": name, "builtin": True}
            for name in _builtin_primitive_names()
            if name.upper() not in _macro_names()
        ],
        "macros": [dict(m) for m in USER_MACROS],
        "hero_tools": [
            {
                "id": t["id"],
                "display_name": t["display_name"],
                "description": t["description"],
                "enabled": bool(HERO_TOOLS_STATE.get(t["id"], False)),
            }
            for t in HERO_TOOLS_CATALOG
        ],
    }


@app.post("/tools/save_macro")
async def save_macro(req: MacroSaveRequest):
    clean_name = (req.name or "").strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Macro name is required.")
    clean_body = (req.body or "").strip()
    if not clean_body:
        raise HTTPException(status_code=400, detail="Macro body is required.")

    spec = {
        "name": clean_name,
        "description": (req.description or "").strip(),
        "params": list(req.params or []),
        "body": clean_body,
    }

    try:
        _register_macro(spec)
    except MacroError as e:
        raise HTTPException(status_code=400, detail=str(e))

    normalized = {
        "name": clean_name.upper(),
        "description": spec["description"],
        "params": [p.upper() for p in spec["params"]],
        "body": clean_body,
    }
    replaced = False
    for idx, existing in enumerate(USER_MACROS):
        if existing["name"] == normalized["name"]:
            USER_MACROS[idx] = normalized
            replaced = True
            break
    if not replaced:
        USER_MACROS.append(normalized)

    _persist_user_macros()
    return {"status": "Success", "macro": normalized, "replaced": replaced}


@app.delete("/tools/macros/{macro_name}")
async def delete_macro(macro_name: str):
    upper = macro_name.upper()
    removed = False
    for idx, existing in enumerate(list(USER_MACROS)):
        if existing["name"] == upper:
            USER_MACROS.pop(idx)
            removed = True
            break
    if not removed:
        raise HTTPException(status_code=404, detail=f"Macro '{macro_name}' not found.")
    kernel.evaluator.registry.pop(upper, None)
    _persist_user_macros()
    return {"status": "Success"}


@app.post("/tools/hero/toggle")
async def toggle_hero_tool(req: HeroToolToggleRequest):
    if req.tool_id not in HERO_TOOLS_STATE:
        raise HTTPException(status_code=404, detail=f"Unknown hero tool '{req.tool_id}'.")
    HERO_TOOLS_STATE[req.tool_id] = bool(req.enabled)
    _persist_hero_tools()
    return {"status": "Success", "tool_id": req.tool_id, "enabled": HERO_TOOLS_STATE[req.tool_id]}
