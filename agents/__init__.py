import json
from pathlib import Path

_AGENTS_DIR = Path(__file__).parent


def load_agents() -> dict[str, dict]:
    agents: dict[str, dict] = {}
    for path in sorted(_AGENTS_DIR.glob("*.json")):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "id" not in data or "system_prompt" not in data:
            raise ValueError(f"Agent file {path.name} is missing required 'id' or 'system_prompt' field.")
        agents[data["id"]] = data
    if "general" not in agents:
        raise ValueError("A 'general' agent is required as the router fallback.")
    return agents
