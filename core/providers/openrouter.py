from .base import Provider, ProviderResponse


class OpenRouterProvider(Provider):
    id = "openrouter"
    display_name = "OpenRouter"

    _BASE_URL = "https://openrouter.ai/api/v1"
    _MAX_TOKENS = 4096

    def __init__(self, api_key: str):
        super().__init__(api_key)
        try:
            import openai  # local import so the package is optional if unused
        except ImportError as e:
            raise RuntimeError(
                "The `openai` package is required for OpenRouter models. "
                "Install it with `pip install openai`."
            ) from e
        self._openai = openai
        self._client = openai.OpenAI(
            api_key=api_key,
            base_url=self._BASE_URL,
            default_headers={
                "HTTP-Referer": "https://github.com/shreydevkar/gridos_kernel",
                "X-Title": "GridOS",
            },
        )

    def generate(
        self,
        *,
        model: str,
        system_instruction: str,
        user_message: str,
        max_output_tokens: int | None = None,
    ) -> ProviderResponse:
        # openai>=2.0 deprecated `max_tokens` in favor of `max_completion_tokens`.
        # Try the new name first, fall back to the legacy name for older SDKs.
        effective_max = max_output_tokens if max_output_tokens is not None else self._MAX_TOKENS
        create_kwargs = dict(
            model=model,
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_message},
            ],
        )
        try:
            response = self._client.chat.completions.create(
                **create_kwargs,
                max_completion_tokens=effective_max,
            )
        except TypeError:
            response = self._client.chat.completions.create(
                **create_kwargs,
                max_tokens=effective_max,
            )

        text = ""
        finish_reason = None
        choices = getattr(response, "choices", None) or []
        if choices:
            message = getattr(choices[0], "message", None)
            text = getattr(message, "content", "") or ""
            finish_reason = getattr(choices[0], "finish_reason", None)

        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None) if usage else None
        completion_tokens = getattr(usage, "completion_tokens", None) if usage else None
        total_tokens = getattr(usage, "total_tokens", None) if usage else None

        return ProviderResponse(
            text=text,
            model=model,
            provider_id=self.id,
            prompt_tokens=prompt_tokens,
            candidates_tokens=completion_tokens,
            total_tokens=total_tokens,
            finish_reason=finish_reason,
            raw=response,
        )

    def is_auth_error(self, exc: BaseException) -> bool:
        try:
            if isinstance(exc, self._openai.AuthenticationError):
                return True
            if isinstance(exc, self._openai.PermissionDeniedError):
                return True
        except Exception:
            pass
        return super().is_auth_error(exc)

    def is_transient_error(self, exc: BaseException) -> bool:
        try:
            if isinstance(exc, self._openai.RateLimitError):
                return True
            if isinstance(exc, self._openai.APIStatusError):
                code = getattr(exc, "status_code", None)
                if isinstance(code, int) and code in {429, 500, 502, 503, 504}:
                    return True
            if isinstance(exc, self._openai.APIConnectionError):
                return True
            if isinstance(exc, self._openai.APITimeoutError):
                return True
        except Exception:
            pass
        return super().is_transient_error(exc)
