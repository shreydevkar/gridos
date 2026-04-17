import json
import os
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
from core.models import AgentIntent, WriteResponse
from core.utils import a1_to_coords


load_dotenv()
client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
MODEL_NAME = "gemini-3.1-flash-lite-preview"
TELEMETRY_PATH = Path("telemetry_log.json")
MAX_CHAIN_ITERATIONS = 3

app = FastAPI(title="GridOS - Agentic Workbook")
kernel = GridOSKernel()
kernel.lock_range("B2", "B10")
AGENTS = load_agents()

os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


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


def call_model(agent_id: str, *, contents, config=None):
    """Wrap generate_content so every call is logged to telemetry_log.json."""
    kwargs: dict = {"model": MODEL_NAME, "contents": contents}
    if config is not None:
        kwargs["config"] = config
    response = client.models.generate_content(**kwargs)

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
    "values": [["..."]]
}
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

    sections = [
        BASE_SYSTEM_RULES,
        f"ACTIVE SHEET: {req.sheet or kernel.active_sheet}\nVIEW SCOPE: {scope_line}\nSELECTED CELLS: {selected_summary}\n{bounds_line}",
        f"CELL METADATA (a1 -> {{val, locked, type}}):\n{context['cell_metadata_json']}",
        f"READABLE GRID STATE:\n{context['formatted_data']}",
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

    intent = AgentIntent(
        agent_id=agent_id,
        target_start_a1=ai_data.get("target_cell", req.selected_cells[0] if req.selected_cells else "C2"),
        data_payload=ai_data.get("values", [["Error"]]),
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
        "values": ai_data.get("values"),
    }


# ---------- Endpoints ----------


@app.get("/")
async def serve_frontend():
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
    return {
        "status": "Success" if requested == actual else "Collision Resolved",
        "sheet": req.sheet or kernel.active_sheet,
        "actual_target": actual,
    }


def _observe_written_cells(preview_cells: list[dict], sheet: str) -> list[dict]:
    """After applying, collect the computed values for any cells the agent just wrote."""
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
        observations.append({
            "cell": a1,
            "value": cell.value,
            "formula": cell.formula,
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

            if _is_completion_signal(values):
                steps.append({
                    "iteration": iteration,
                    "agent_id": preview["agent_id"],
                    "reasoning": preview["reasoning"],
                    "target": preview["original_request"],
                    "values": values,
                    "observations": [],
                    "completion_signal": True,
                })
                break

            intent = AgentIntent(
                agent_id=preview["agent_id"],
                target_start_a1=preview["original_request"],
                data_payload=values,
                shift_direction="right",
            )
            _, actual_target = kernel.process_agent_intent(intent, sheet)
            observations = _observe_written_cells(preview["preview_cells"], sheet)
            formula_observations = [o for o in observations if o["formula"]]

            steps.append({
                "iteration": iteration,
                "agent_id": preview["agent_id"],
                "reasoning": preview["reasoning"],
                "target": actual_target,
                "values": values,
                "observations": observations,
                "completion_signal": False,
            })

            history.append({"role": "user", "content": current_prompt})
            history.append({"role": "assistant", "content": json.dumps({
                "reasoning": preview["reasoning"],
                "target": actual_target,
                "values": values,
            })})

            if not formula_observations:
                break

            summary = ", ".join(f"{o['cell']}={o['value']}" for o in formula_observations)
            current_prompt = (
                f"The previous operation resulted in [{summary}]. "
                "If the ORIGINAL task (see the user message at the top of this conversation) has more targets "
                "left to write, produce the next one now and do NOT repeat cells you have already written. "
                "If every part of the original task is done, signal completion by returning values=[[\"\"]] "
                "with target_cell equal to the last written cell."
            )

        return {
            "sheet": sheet,
            "steps": steps,
            "iterations_used": len(steps),
            "terminated_early": len(steps) < max_iters,
        }
    except Exception as e:
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
    return {"sheet": target, "cells": kernel.export_sheet(target)}


@app.get("/workbook")
async def get_workbook():
    return {
        "active_sheet": kernel.active_sheet,
        "sheets": kernel.list_sheets(),
    }


@app.post("/workbook/sheet")
async def create_sheet(req: SheetCreateRequest):
    name = kernel.create_sheet(req.name)
    kernel.lock_range("B2", "B10", sheet_name=name)
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
