"""OpenAI Chat Completions with function calling (openai SDK 3.x).

Also talks to OpenAI-compatible servers (Gemini, Ollama) through `base_url`. Those get only the
parameters their docs list: no `tool_choice`, no `parallel_tool_calls`, `max_tokens` instead of
`max_completion_tokens`.
"""

import json
import time
from typing import Any

import openai
from openai import AsyncOpenAI

from app.core.logging import get_logger
from app.llm.base import ChatMessage, LLMError, LLMResponse, LLMTimeout, ToolCall, ToolSpec

log = get_logger(__name__)

# Failures of the service, not of our request: a second model may well answer. A bad request or
# a wrong key (4xx other than 429) would fail the same way, so those are not retried.
TRANSIENT = (
    openai.RateLimitError,
    openai.InternalServerError,
    openai.APIConnectionError,  # includes APITimeoutError
)


class OpenAIProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_s: float = 25.0,
        temperature: float | None = None,
        max_completion_tokens: int = 600,
        base_url: str | None = None,
        fallback_model: str | None = None,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.model_name = model
        self._fallback_model = fallback_model or None
        self._temperature = temperature
        self._max_tokens = max_completion_tokens
        self.native = is_openai(base_url)
        self._client = client or AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_s,
            # With a fallback model the fallback is the retry: a rate-limited or hanging first
            # model must not cost two full timeouts before the second one is tried.
            max_retries=0 if fallback_model else 1,
        )

    async def complete(self, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMResponse:
        kwargs: dict[str, Any] = {"messages": [_to_openai(m) for m in messages]}
        if self.native:
            kwargs["max_completion_tokens"] = self._max_tokens
        else:
            kwargs["max_tokens"] = self._max_tokens
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
            if self.native:
                kwargs["tool_choice"] = "auto"
                # One tool call per step keeps side effects ordered and easy to stage (R12).
                # Compatible servers may return several calls; the orchestrator runs only the
                # first one per step, so the order holds there too.
                kwargs["parallel_tool_calls"] = False
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature

        started = time.perf_counter()
        try:
            response = await self._create(self.model_name, kwargs)
        except (LLMTimeout, LLMError) as exc:
            if self._fallback_model is None or not exc.transient:
                raise
            log.warning("llm.fallback", primary=self.model_name, error=type(exc).__name__)
            response = await self._create(self._fallback_model, kwargs)
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
            # The SDK keeps unknown fields such as Gemini's extra_content in model_extra.
            extra = getattr(tc, "extra_content", None)
            if extra is None:
                extra = (getattr(tc, "model_extra", None) or {}).get("extra_content")
            calls.append(
                ToolCall(
                    tc.id,
                    function.name,
                    parsed if isinstance(parsed, dict) else {},
                    raw,
                    extra=extra if isinstance(extra, dict) else None,
                )
            )
        usage = response.usage
        return LLMResponse(
            content=message.content,
            tool_calls=calls,
            tokens_in=usage.prompt_tokens if usage else 0,
            tokens_out=usage.completion_tokens if usage else 0,
            model=response.model or self._used_model,
            latency_ms=latency_ms,
        )

    async def _create(self, model: str, kwargs: dict[str, Any]):
        self._used_model = model
        try:
            return await self._client.chat.completions.create(model=model, **kwargs)
        except openai.APITimeoutError as exc:
            raise LLMTimeout(str(exc), transient=True) from exc
        except openai.APIError as exc:
            raise LLMError(
                f"{type(exc).__name__}: {exc}", transient=isinstance(exc, TRANSIENT)
            ) from exc


def is_openai(base_url: str | None) -> bool:
    return base_url is None or "api.openai.com" in base_url


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
                    **({"extra_content": call.extra} if call.extra else {}),
                }
                for call in message.tool_calls
            ],
        }
    if message.role == "tool":
        return {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content}
    return {"role": message.role, "content": message.content or ""}
