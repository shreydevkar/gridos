"""Static catalog of model IDs that GridOS knows how to route.

Each entry: {id, provider, display_name, description}.
- `id` is the string sent to the provider SDK.
- `provider` is the short stable provider id ("gemini" | "anthropic" | "openrouter" | "groq").

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
    {
        "id": "nousresearch/hermes-3-llama-3.1-405b:free",
        "provider": "openrouter",
        "display_name": "Hermes 3 Llama 3.1 405B (free)",
        "description": "Free via OpenRouter. Large 405B reasoning model.",
    },
    {
        "id": "meta-llama/llama-3.3-70b-instruct:free",
        "provider": "openrouter",
        "display_name": "Llama 3.3 70B Instruct (free)",
        "description": "Free via OpenRouter. Strong general-purpose Llama.",
    },
    {
        "id": "meta-llama/llama-3.2-3b-instruct:free",
        "provider": "openrouter",
        "display_name": "Llama 3.2 3B Instruct (free)",
        "description": "Free via OpenRouter. Tiny + fast; good for quick edits.",
    },
    {
        "id": "openrouter/free",
        "provider": "openrouter",
        "display_name": "Free Models Router (auto)",
        "description": "Free via OpenRouter. Auto-routes to a random free model that supports the request.",
    },
    {
        "id": "openai/gpt-oss-120b",
        "provider": "groq",
        "display_name": "GPT-OSS 120B (Groq)",
        "description": "OpenAI open-weights 120B via Groq. Best instruction-following among Groq options.",
    },
    {
        "id": "openai/gpt-oss-20b",
        "provider": "groq",
        "display_name": "GPT-OSS 20B (Groq, fast)",
        "description": "OpenAI open-weights 20B via Groq at ~1000 tps. Fastest Groq model.",
    },
    {
        "id": "qwen/qwen3-32b",
        "provider": "groq",
        "display_name": "Qwen3 32B (Groq)",
        "description": "Qwen3 32B via Groq. Strong at structured output (preview; may change).",
    },
    {
        "id": "llama-3.3-70b-versatile",
        "provider": "groq",
        "display_name": "Llama 3.3 70B (Groq)",
        "description": "Llama 3.3 70B via Groq. Capable but sometimes prefaces JSON with prose.",
    },
    {
        "id": "llama-3.1-8b-instant",
        "provider": "groq",
        "display_name": "Llama 3.1 8B (Groq, instant)",
        "description": "Llama 3.1 8B via Groq. Tiny + extremely fast; good for router/classifier calls.",
        # Router-only — Groq's free tier caps this model at 6K TPM, which is
        # below our agent system_instruction size (~3.5K finance prompt + ~1K
        # output spec + grid + history). Hidden from the chat composer's model
        # picker; the router still reaches it via _ROUTER_MODEL_PREFERENCE.
        "router_only": True,
    },
]

_FALLBACK_BY_PROVIDER = {
    "gemini": "gemini-3.1-flash-lite-preview",
    "anthropic": "claude-haiku-4-5-20251001",
    "groq": "openai/gpt-oss-120b",
    "openrouter": "openrouter/free",
}


def get_model_entry(model_id: str) -> Optional[dict]:
    for entry in MODEL_CATALOG:
        if entry["id"] == model_id:
            return entry
    return None


def default_model_id(available_providers: set[str]) -> Optional[str]:
    """Pick a sensible default from the first provider that has a key configured.

    Order: Gemini → Anthropic → Groq → OpenRouter. Gemini leads because Flash
    Lite's free tier has ~250K TPM — 30x what Groq's free tier gives even on
    their biggest models — so first-time users with a fresh Gemini key can
    actually build a DCF or 3-statement model without hitting TPM 413s. Groq
    is faster per-token but its 6–8K TPM free-tier cap can't fit our typical
    agent prompt + multi-intent JSON output.
    """
    if "gemini" in available_providers:
        return _FALLBACK_BY_PROVIDER["gemini"]
    if "anthropic" in available_providers:
        return _FALLBACK_BY_PROVIDER["anthropic"]
    if "groq" in available_providers:
        return _FALLBACK_BY_PROVIDER["groq"]
    if "openrouter" in available_providers:
        return _FALLBACK_BY_PROVIDER["openrouter"]
    return None
