from pydantic import BaseModel, Field
from typing import Optional, Any, Dict, Literal

class CellState(BaseModel):
    value: Any = None
    formula: Optional[str] = None
    datatype: str = "string"  # string, int, float, boolean, loading
    locked: bool = False
    agent_owner: Optional[str] = None
    # Display-only decimal precision. None = render the raw value verbatim.
    # The stored `value` is never rounded — formatting is applied in the
    # frontend at render time so downstream formula references stay precise.
    decimals: Optional[int] = None

class AgentIntent(BaseModel):
    agent_id: str
    target_start_a1: str
    data_payload: list[list[Any]]  # 2D array of data the agent wants to write
    priority: int = 1
    shift_direction: str = "right" # If collision, move 'right' (cols) or 'down' (rows)

class WriteResponse(BaseModel):
    status: str
    original_target: str
    actual_target: str
    message: str

class ChartSpec(BaseModel):
    id: str
    anchor_cell: str = "F2"
    data_range: str  # e.g. "A1:B6"
    chart_type: Literal["bar", "line", "pie"] = "bar"
    title: str = ""
    width: int = 400
    height: int = 280
    orientation: Literal["columns", "rows"] = "columns"  # whether series run down columns or across rows