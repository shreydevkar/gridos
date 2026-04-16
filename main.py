import json
import os

import google.generativeai as genai
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Dict, List, Optional

from core.engine import GridOSKernel
from core.functions import FormulaEvaluator
from core.models import AgentIntent, WriteResponse


genai.configure(api_key="REDACTED")
model = genai.GenerativeModel("gemini-3.1-flash-lite-preview")

app = FastAPI(title="GridOS - Agentic Workbook")
kernel = GridOSKernel()
kernel.lock_range("B2", "B10")

os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def serve_frontend():
    return FileResponse("static/index.html")


class ChatRequest(BaseModel):
    prompt: str
    history: List[Dict[str, str]] = []
    scope: str = "sheet"
    selected_cells: List[str] = []
    sheet: Optional[str] = None


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


def build_system_instruction(task_category: str, context: dict, req: ChatRequest) -> str:
    selected_summary = ", ".join(req.selected_cells) if req.selected_cells else "No cells selected."
    scope_line = "Selected cells only" if req.scope == "selection" else "Entire active sheet"

    if "finance" in task_category:
        return f"""
        You are a Senior Financial Analyst Agent.

        ACTIVE SHEET: {req.sheet or kernel.active_sheet}
        VIEW SCOPE: {scope_line}
        SELECTED CELLS: {selected_summary}
        CONTEXT (Current Grid State):
        {context['formatted_data']}

        ALREADY OCCUPIED CELLS IN SCOPE: {context['occupied_info']}

        RULES:
        1. If the scope is "selection", prioritize editing or building near the selected cells unless the user explicitly asks otherwise.
        2. If the user asks to place raw numbers, place them. If they ask for math, calculate it.
        3. Use GridOS supported formulas: =SUM(A, B), =MAX(A, B), =MIN(A, B), =MINUS(A, B).
        4. Return a 2D 'values' array. Horizontal writes use [[A, B]], vertical writes use [[A], [B]].
        5. Keep the response preview-safe. Do not describe that changes were already committed.

        OUTPUT FORMAT: strictly valid JSON
        {{
            "reasoning": "Short explanation.",
            "target_cell": "C3",
            "values": [[1000]]
        }}
        """

    return f"""
    You are a General Data Assistant.

    ACTIVE SHEET: {req.sheet or kernel.active_sheet}
    VIEW SCOPE: {scope_line}
    SELECTED CELLS: {selected_summary}
    CONTEXT (Current Grid State):
    {context['formatted_data']}

    RULES:
    1. If the scope is "selection", focus on the selected cells unless the user clearly wants a full-sheet action.
    2. Follow the user's instructions for text manipulation, clearing, labeling, or basic data entry.
    3. Return a 2D 'values' array and a single top-left target_cell.
    4. Keep the response preview-safe. Do not claim anything was already written.

    OUTPUT FORMAT: strictly valid JSON
    {{
        "reasoning": "Short explanation.",
        "target_cell": "A1",
        "values": [["Q1 Report"]]
    }}
    """


def generate_agent_preview(req: ChatRequest):
    sheet = req.sheet or kernel.active_sheet
    context = kernel.get_context_for_ai(sheet, req.selected_cells, req.scope)
    history_context = "\n".join([f"{h['role'].upper()}: {h['content']}" for h in req.history])

    router_instruction = f"""
    Analyze this user task: "{req.prompt}".
    Previous context: {history_context}
    Is this task "general" or "finance"?
    Return ONLY the word "general" or "finance".
    """

    router_res = model.generate_content(router_instruction)
    task_category = router_res.text.strip().lower()
    system_instruction = build_system_instruction(task_category, context, req)

    final_response = model.generate_content([system_instruction, req.prompt])
    clean_json = final_response.text.replace("```json", "").replace("```", "").strip()
    ai_data = json.loads(clean_json)

    intent = AgentIntent(
        agent_id="Finance-Bot" if "finance" in task_category else "General-Bot",
        target_start_a1=ai_data.get("target_cell", req.selected_cells[0] if req.selected_cells else "C2"),
        data_payload=ai_data.get("values", [["Error"]]),
        shift_direction="right",
    )

    preview = kernel.preview_agent_intent(intent, sheet)
    return {
        "category": task_category,
        "reasoning": ai_data.get("reasoning"),
        "sheet": sheet,
        "scope": req.scope,
        "selected_cells": req.selected_cells,
        "agent_id": intent.agent_id,
        "target_cell": preview["actual_target"],
        "original_request": preview["original_target"],
        "preview_cells": preview["preview_cells"],
        "values": ai_data.get("values"),
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


@app.post("/formula/evaluate")
async def evaluate_formula(req: FormulaRequest):
    evaluator = FormulaEvaluator()
    return {"result": evaluator.evaluate(req.function_name, req.arguments)}


@app.post("/system/clear")
async def clear_grid(sheet: Optional[str] = None):
    kernel.clear_unlocked(sheet)
    return {"status": "Success", "sheet": sheet or kernel.active_sheet}
