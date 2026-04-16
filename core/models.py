from pydantic import BaseModel, Field
from typing import Optional, Any, Dict

class CellState(BaseModel):
    value: Any = None
    formula: Optional[str] = None
    datatype: str = "string"  # string, int, float, boolean, loading
    locked: bool = False
    agent_owner: Optional[str] = None

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