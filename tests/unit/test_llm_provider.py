"""OpenAI provider in native mode and against OpenAI-compatible servers (Gemini, Ollama)."""

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.llm.base import ChatMessage, ToolSpec
from app.llm.factory import LLMConfigError, build_llm
from app.llm.openai_provider import OpenAIProvider

GEMINI = "https://generativelanguage.googleapis.com/v1beta/openai/"
OLLAMA = "http://localhost:11434/v1/"
TOOL = ToolSpec("get_free_slots", "Find free slots", {"type": "object", "properties": {}})
MESSAGES = [ChatMessage("system", "rules"), ChatMessage("user", "Є вікно завтра?")]
SIGNATURE = {"google": {"thought_signature": "sig-123"}}


class FakeCompletions:
    """Records the request and answers with one tool call and no usage block."""

    def __init__(self) -> None:
        self.kwargs: dict = {}

    async def create(self, **kwargs):
        self.kwargs = kwargs
        call = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(name="get_free_slots", arguments='{"service_id": "gel"}'),
            extra_content=SIGNATURE,
        )
        message = SimpleNamespace(content=None, tool_calls=[call])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None, model="")


def provider(base_url: str | None) -> tuple[OpenAIProvider, FakeCompletions]:
    completions = FakeCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return OpenAIProvider(api_key="k", model="m", base_url=base_url, client=client), completions


@pytest.fixture(autouse=True)
def no_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENAI_API_KEY", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_PROVIDER"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("base_url", [None, "https://api.openai.com/v1"])
async def test_openai_gets_one_tool_call_per_step(base_url: str | None) -> None:
    llm, completions = provider(base_url)
    response = await llm.complete(MESSAGES, [TOOL])
    sent = completions.kwargs
    assert sent["parallel_tool_calls"] is False
    assert sent["tool_choice"] == "auto"
    assert sent["max_completion_tokens"] == 600 and "max_tokens" not in sent
    assert response.tool_calls[0].name == "get_free_slots"
    assert response.tool_calls[0].arguments == {"service_id": "gel"}
    assert (response.tokens_in, response.tokens_out, response.model) == (0, 0, "m")


@pytest.mark.parametrize("base_url", [GEMINI, OLLAMA])
async def test_compatible_server_gets_only_documented_parameters(base_url: str) -> None:
    llm, completions = provider(base_url)
    response = await llm.complete(MESSAGES, [TOOL])
    sent = completions.kwargs
    assert "parallel_tool_calls" not in sent and "tool_choice" not in sent
    assert sent["max_tokens"] == 600 and "max_completion_tokens" not in sent
    assert sent["tools"][0]["function"]["name"] == "get_free_slots"
    assert response.tool_calls[0].name == "get_free_slots"


def test_factory_needs_a_model_and_a_key_for_openai() -> None:
    with pytest.raises(LLMConfigError):
        build_llm(Settings(_env_file=None, llm_model="gpt-x"))
    with pytest.raises(LLMConfigError):
        build_llm(Settings(_env_file=None, openai_api_key="sk-test"))


def test_factory_points_the_client_at_gemini_with_its_key() -> None:
    settings = Settings(
        _env_file=None,
        llm_model="gemini-2.5-flash",
        llm_base_url=GEMINI,
        llm_api_key="gemini-key",
        openai_api_key="openai-key",
    )
    llm = build_llm(settings)
    assert not llm.native
    assert str(llm._client.base_url) == GEMINI
    assert llm._client.api_key == "gemini-key"


def test_factory_allows_ollama_without_a_key() -> None:
    llm = build_llm(Settings(_env_file=None, llm_model="qwen3", llm_base_url=OLLAMA))
    assert str(llm._client.base_url) == OLLAMA
    assert llm._client.api_key == "not-needed"


async def test_gemini_thought_signature_goes_back_with_the_call() -> None:
    llm, completions = provider(GEMINI)
    response = await llm.complete(MESSAGES, [TOOL])
    call = response.tool_calls[0]
    assert call.extra == SIGNATURE
    follow_up = [*MESSAGES, ChatMessage("assistant", None, tool_calls=[call])]
    follow_up.append(ChatMessage("tool", '{"slots": []}', tool_call_id=call.id))
    await llm.complete(follow_up, [TOOL])
    sent_call = completions.kwargs["messages"][2]["tool_calls"][0]
    assert sent_call["extra_content"] == SIGNATURE
