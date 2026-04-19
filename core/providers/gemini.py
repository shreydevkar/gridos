from google import genai
from google.genai import types

from .base import Provider, ProviderResponse


class GeminiProvider(Provider):
    id = "gemini"
    display_name = "Google Gemini"

    def __init__(self, api_key: str):
        super().__init__(api_key)
        self._client = genai.Client(api_key=api_key)

    def generate(
        self,
        *,
        model: str,
        system_instruction: str,
        user_message: str,
        max_output_tokens: int | None = None,
    ) -> ProviderResponse:
        config_kwargs = {"system_instruction": system_instruction}
        if max_output_tokens is not None:
            config_kwargs["max_output_tokens"] = max_output_tokens
        config = types.GenerateContentConfig(**config_kwargs)
        response = self._client.models.generate_content(
            model=model,
            contents=user_message,
            config=config,
        )
        usage = getattr(response, "usage_metadata", None)
        finish_reason = None
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            fr = getattr(candidates[0], "finish_reason", None)
            if fr is not None:
                finish_reason = getattr(fr, "name", None) or str(fr)
        return ProviderResponse(
            text=response.text or "",
            model=model,
            provider_id=self.id,
            prompt_tokens=getattr(usage, "prompt_token_count", None) if usage else None,
            candidates_tokens=getattr(usage, "candidates_token_count", None) if usage else None,
            total_tokens=getattr(usage, "total_token_count", None) if usage else None,
            finish_reason=finish_reason,
            raw=response,
        )
