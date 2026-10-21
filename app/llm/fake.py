"""Scripted LLM for tests and offline demos: returns prepared responses in order."""

import itertools
from collections import deque
from collections.abc import Callable
from typing import Any

from app.llm.base import ChatMessage, LLMResponse, ToolCall, ToolSpec

Step = LLMResponse | Exception | Callable[[list[ChatMessage], list[ToolSpec]], LLMResponse]

_ids = itertools.count(1)


def say(text: str) -> LLMResponse:
    return LLMResponse(content=text, tokens_in=100, tokens_out=20, model="fake")


def call(name: str, **arguments: Any) -> LLMResponse:
    tool_call = ToolCall(id=f"call_{next(_ids)}", name=name, arguments=arguments)
    return LLMResponse(
        content=None, tool_calls=[tool_call], tokens_in=100, tokens_out=10, model="fake"
    )


class FakeLLM:
    model_name = "fake"

    def __init__(self, steps: list[Step] | None = None) -> None:
        self.steps: deque[Step] = deque(steps or [])
        self.calls: list[tuple[list[ChatMessage], list[ToolSpec]]] = []

    def add(self, *steps: Step) -> None:
        self.steps.extend(steps)

    async def complete(self, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMResponse:
        self.calls.append((list(messages), list(tools)))
        if not self.steps:
            raise AssertionError("FakeLLM: no scripted response left")
        step = self.steps.popleft()
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step(messages, tools)
        return step

    @property
    def last_system_prompt(self) -> str:
        messages, _ = self.calls[-1]
        return messages[0].content or ""
