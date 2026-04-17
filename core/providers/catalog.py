"""Static catalog of model IDs that GridOS knows how to route.

Each entry: {id, provider, display_name, description}.
- `id` is the string sent to the provider SDK.
- `provider` is the short stable provider id ("gemini" | "anthropic").

Add new models by appending to MODEL_CATALOG. The UI picks them up automatically
as long as the corresponding provider has a configured API key.
"""
from typing import Optional

MODEL_CATALOG: list[dict] = [
    {
        "id": "gemini-3.1-flash-lite-preview",
        "provider": "gemini",
        "display_name": "Gemini 3.1 Flash Lite",
        "description": "Fast + cheap. Default for routing and most grid edits.",
    },
    {
        "id": "gemini-3.1-pro",
        "provider": "gemini",
        "display_name": "Gemini 3.1 Pro",
        "description": "Higher-quality Gemini; slower, more expensive.",
    },
    {
        "id": "claude-haiku-4-5-20251001",
        "provider": "anthropic",
        "display_name": "Claude Haiku 4.5",
        "description": "Fast and cheap Claude model. Good for quick edits.",
    },
    {
        "id": "claude-sonnet-4-6",
        "provider": "anthropic",
        "display_name": "Claude Sonnet 4.6",
        "description": "Balanced Claude model. Strong reasoning at mid cost.",
    },
    {
        "id": "claude-opus-4-7",
        "provider": "anthropic",
        "display_name": "Claude Opus 4.7",
        "description": "Most capable Claude model. Best for complex models.",
    },
]

_FALLBACK_BY_PROVIDER = {
    "gemini": "gemini-3.1-flash-lite-preview",
    "anthropic": "claude-haiku-4-5-20251001",
}


def get_model_entry(model_id: str) -> Optional[dict]:
    for entry in MODEL_CATALOG:
        if entry["id"] == model_id:
            return entry
    return None


def default_model_id(available_providers: set[str]) -> Optional[str]:
    """Pick a sensible default from the first provider that has a key configured."""
    if "gemini" in available_providers:
        return _FALLBACK_BY_PROVIDER["gemini"]
    if "anthropic" in available_providers:
        return _FALLBACK_BY_PROVIDER["anthropic"]
    return None
