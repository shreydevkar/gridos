from dataclasses import dataclass, field
from typing import Optional


class ProviderError(Exception):
    """Base class for provider-level errors surfaced to the caller."""


class ProviderAuthError(ProviderError):
    """The provider rejected the API key (401/403 or equivalent)."""


class ProviderTransientError(ProviderError):
    """Retryable error (rate limit, overload, gateway). Caller may back off."""


@dataclass
class ProviderResponse:
    text: str
    model: str
    provider_id: str
    prompt_tokens: Optional[int] = None
    candidates_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    finish_reason: Optional[str] = None
    raw: object = field(default=None, repr=False)


class Provider:
    """Provider interface. Subclasses implement generate() and classify errors."""

    id: str = ""  # short stable id, e.g. "gemini", "anthropic"
    display_name: str = ""

    def __init__(self, api_key: str):
        self.api_key = api_key

    def generate(
        self,
        *,
        model: str,
        system_instruction: str,
        user_message: str,
        max_output_tokens: Optional[int] = None,
    ) -> ProviderResponse:
        raise NotImplementedError

    def is_transient_error(self, exc: BaseException) -> bool:
        code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        if isinstance(code, int) and code in {429, 500, 502, 503, 504}:
            return True
        msg = str(exc).lower()
        return any(
            s in msg
            for s in (
                "429", "503", "500", "502", "504",
                "unavailable", "overloaded", "rate limit",
                "resource exhausted", "timeout", "connection",
            )
        )

    def is_auth_error(self, exc: BaseException) -> bool:
        code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        if isinstance(code, int) and code in {401, 403}:
            return True
        msg = str(exc).lower()
        return any(
            s in msg
            for s in (
                "unauthorized", "invalid api key", "api key not valid",
                "authentication", "permission denied", "401", "403",
            )
        )
