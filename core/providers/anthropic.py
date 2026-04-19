from .base import Provider, ProviderResponse


class AnthropicProvider(Provider):
    id = "anthropic"
    display_name = "Anthropic Claude"

    _MAX_TOKENS = 4096

    def __init__(self, api_key: str):
        super().__init__(api_key)
        try:
            import anthropic  # local import so the package is optional if unused
        except ImportError as e:
            raise RuntimeError(
                "The `anthropic` package is required for Claude models. "
                "Install it with `pip install anthropic`."
            ) from e
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key)

    def generate(
        self,
        *,
        model: str,
        system_instruction: str,
        user_message: str,
        max_output_tokens: int | None = None,
    ) -> ProviderResponse:
        # Claude doesn't have a JSON-mode flag; we rely on the system prompt's
        # "OUTPUT FORMAT: strictly valid JSON" contract. `_parse_ai_response`
        # on the caller side strips ``` fences if the model wraps them.
        effective_max = max_output_tokens if max_output_tokens is not None else self._MAX_TOKENS
        response = self._client.messages.create(
            model=model,
            max_tokens=effective_max,
            system=system_instruction,
            messages=[{"role": "user", "content": user_message}],
        )

        text_parts = []
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "text":
                text_parts.append(getattr(block, "text", "") or "")
        text = "".join(text_parts)

        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", None) if usage else None
        output_tokens = getattr(usage, "output_tokens", None) if usage else None
        total = (
            (input_tokens or 0) + (output_tokens or 0)
            if (input_tokens is not None or output_tokens is not None)
            else None
        )

        return ProviderResponse(
            text=text,
            model=model,
            provider_id=self.id,
            prompt_tokens=input_tokens,
            candidates_tokens=output_tokens,
            total_tokens=total,
            finish_reason=getattr(response, "stop_reason", None),
            raw=response,
        )

    def is_auth_error(self, exc: BaseException) -> bool:
        # anthropic.AuthenticationError is a concrete class; fall back to base heuristics.
        try:
            if isinstance(exc, self._anthropic.AuthenticationError):
                return True
            if isinstance(exc, self._anthropic.PermissionDeniedError):
                return True
        except Exception:
            pass
        return super().is_auth_error(exc)

    def is_transient_error(self, exc: BaseException) -> bool:
        try:
            if isinstance(exc, self._anthropic.RateLimitError):
                return True
            if isinstance(exc, self._anthropic.APIStatusError):
                code = getattr(exc, "status_code", None)
                if isinstance(code, int) and code in {429, 500, 502, 503, 504}:
                    return True
            if isinstance(exc, self._anthropic.APIConnectionError):
                return True
            if isinstance(exc, self._anthropic.APITimeoutError):
                return True
        except Exception:
            pass
        return super().is_transient_error(exc)
