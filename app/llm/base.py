"""Provider-neutral LLM interface. Swapping the model is an env change, not a code change."""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""
    # Provider data that must travel back unchanged with the call, e.g. Gemini's
    # extra_content.google.thought_signature; without it Gemini rejects the next request.
    extra: dict[str, Any] | None = None


@dataclass
class ChatMessage:
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    latency_ms: int = 0


class LLMError(Exception):
    """The provider failed; the caller answers with a safe template and hands off."""


class LLMTimeout(LLMError):
    pass


class LLMProvider(Protocol):
    model_name: str

    async def complete(self, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMResponse: ...
