from app.config import Settings
from app.llm.base import LLMProvider


class LLMConfigError(Exception):
    pass


def build_llm(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "fake":
        from app.llm.fake import FakeLLM

        return FakeLLM()
    if not settings.openai_api_key or not settings.llm_model:
        raise LLMConfigError("Set OPENAI_API_KEY and LLM_MODEL in .env (see .env.example).")
    from app.llm.openai_provider import OpenAIProvider

    return OpenAIProvider(
        api_key=settings.openai_api_key.get_secret_value(),
        model=settings.llm_model,
        timeout_s=settings.llm_timeout_s,
        temperature=settings.llm_temperature,
        max_completion_tokens=settings.llm_max_completion_tokens,
    )
