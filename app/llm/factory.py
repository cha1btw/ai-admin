from app.config import Settings
from app.llm.base import ChatMessage, LLMError, LLMProvider, LLMResponse, ToolSpec


class LLMConfigError(Exception):
    pass


class UnavailableLLM:
    """Used when the model is not configured: every call fails, so clients get the safe
    template and a handoff, while Owner Bot and everything else keep working."""

    model_name = "unavailable"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def complete(self, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMResponse:
        raise LLMError(self.reason)


def build_llm(
    settings: Settings, *, max_completion_tokens: int | None = None, timeout_s: float | None = None
) -> LLMProvider:
    """The overrides serve long one-off jobs such as the studio import in the admin panel."""
    if settings.llm_provider == "fake":
        from app.llm.fake import FakeLLM

        return FakeLLM()
    key = settings.llm_api_key or settings.openai_api_key
    if not settings.llm_model or (key is None and not settings.llm_base_url):
        raise LLMConfigError(
            "Set LLM_MODEL and OPENAI_API_KEY, or LLM_BASE_URL with LLM_API_KEY, in .env "
            "(see .env.example)."
        )
    from app.llm.openai_provider import OpenAIProvider

    return OpenAIProvider(
        # Ollama needs no key, but the SDK refuses an empty one.
        api_key=key.get_secret_value() if key else "not-needed",
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout_s=timeout_s or settings.llm_timeout_s,
        temperature=settings.llm_temperature,
        max_completion_tokens=max_completion_tokens or settings.llm_max_completion_tokens,
    )
