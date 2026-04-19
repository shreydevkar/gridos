from .base import Provider, ProviderResponse


class GroqProvider(Provider):
    id = "groq"
    display_name = "Groq"

    _BASE_URL = "https://api.groq.com/openai/v1"
    # Multi-intent agent responses (e.g. a 25-rectangle 3-statement model)
    # comfortably push 6–8K tokens of JSON. Was 16384, but Groq's free-tier
    # TPM bucket counts the RESERVED max_completion_tokens up-front — a 16K
    # reservation on a 3.5K prompt = ~20K "requested", which blew past the
    # 8K free-tier cap on gpt-oss-120b even for simple DCF prompts. 8192
    # still clears multi-intent JSON in practice while halving the up-front
    # TPM reservation. Paying-tier users can raise this per call via the
    # `max_output_tokens` kwarg if they hit `finish_reason=length`.
    _MAX_TOKENS = 8192

    def __init__(self, api_key: str):
        super().__init__(api_key)
        try:
            import openai  # shared with OpenRouter; both speak OpenAI-compatible
        except ImportError as e:
            raise RuntimeError(
                "The `openai` package is required for Groq models. "
                "Install it with `pip install openai`."
            ) from e
        self._openai = openai
        self._client = openai.OpenAI(api_key=api_key, base_url=self._BASE_URL)

    def generate(
        self,
        *,
        model: str,
        system_instruction: str,
        user_message: str,
        max_output_tokens: int | None = None,
    ) -> ProviderResponse:
        # Groq's free-tier TPM counts RESERVED max_completion_tokens against the
        # bucket up-front, so always using _MAX_TOKENS=16384 instantly blows the
        # 6K TPM cap on tiny models like llama-3.1-8b even for router-sized
        # prompts. Honor the caller's override when given (router passes ~64).
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
