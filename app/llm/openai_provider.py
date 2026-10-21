"""OpenAI Chat Completions with function calling (openai SDK 3.x)."""

import json
import time
from typing import Any

import openai
from openai import AsyncOpenAI

from app.llm.base import ChatMessage, LLMError, LLMResponse, LLMTimeout, ToolCall, ToolSpec


class OpenAIProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_s: float = 25.0,
        temperature: float | None = None,
        max_completion_tokens: int = 600,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.model_name = model
        self._temperature = temperature
        self._max_tokens = max_completion_tokens
        self._client = client or AsyncOpenAI(api_key=api_key, timeout=timeout_s, max_retries=1)

    async def complete(self, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model_name,
            "messages": [_to_openai(m) for m in messages],
            "max_completion_tokens": self._max_tokens,
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
            kwargs["tool_choice"] = "auto"
            # One tool call per step keeps side effects ordered and easy to stage (R12).
            kwargs["parallel_tool_calls"] = False
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature

        started = time.perf_counter()
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except openai.APITimeoutError as exc:
            raise LLMTimeout(str(exc)) from exc
        except openai.APIError as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        if not response.choices:
            raise LLMError("empty response")
        message = response.choices[0].message
        calls: list[ToolCall] = []
        for tc in message.tool_calls or []:
            function = getattr(tc, "function", None)
            if function is None:
                continue
            raw = function.arguments or "{}"
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"_invalid_json": raw}
            calls.append(
                ToolCall(tc.id, function.name, parsed if isinstance(parsed, dict) else {}, raw)
            )
        usage = response.usage
        return LLMResponse(
            content=message.content,
            tool_calls=calls,
            tokens_in=usage.prompt_tokens if usage else 0,
            tokens_out=usage.completion_tokens if usage else 0,
            model=response.model or self.model_name,
            latency_ms=latency_ms,
        )


def _to_openai(message: ChatMessage) -> dict[str, Any]:
    if message.role == "assistant" and message.tool_calls:
        return {
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.raw_arguments
                        or json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ],
        }
    if message.role == "tool":
        return {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content}
    return {"role": message.role, "content": message.content or ""}
