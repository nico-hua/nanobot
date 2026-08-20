"""Anthropic-compatible implementation of the LLM provider interface."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from anthropic import AsyncAnthropic

from .base import LLMProvider, LLMResponse, ProviderError, TokenUsage
from .messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolCallRequest,
    ToolMessage,
)


class AnthropicCompatProvider(LLMProvider):
    """Call APIs that implement the Anthropic Messages protocol."""

    def __init__(
        self,
        api_key: str,
        api_base: str,
        default_model: str,
        default_max_tokens: int = 1024,
        default_thinking: Mapping[str, Any] | None = None,
        *,
        client: Any | None = None,
    ) -> None:
        self.api_base = api_base
        self.default_model = default_model
        self.default_max_tokens = default_max_tokens
        self.default_thinking = (
            dict(default_thinking) if default_thinking is not None else None
        )
        self._client = client if client is not None else AsyncAnthropic(
            api_key=api_key,
            base_url=api_base,
        )

    async def complete(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Mapping[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        request = self._build_request(messages, tools, max_tokens, temperature)

        try:
            response = await self._client.messages.create(**request)
            return _response_from_message(response)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("Anthropic-compatible completion failed") from exc

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Mapping[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        request = self._build_request(messages, tools, max_tokens, temperature)

        try:
            async with self._client.messages.stream(**request) as stream:
                async for delta in stream.text_stream:
                    if on_delta is not None:
                        await on_delta(delta)
                response = await stream.get_final_message()
            return _response_from_message(response)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("Anthropic-compatible streaming failed") from exc

    def _build_request(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Mapping[str, Any]] | None,
        max_tokens: int | None,
        temperature: float | None,
    ) -> dict[str, Any]:
        system_parts: list[str] = []
        request_messages: list[dict[str, Any]] = []
        for message in messages:
            if isinstance(message, SystemMessage):
                system_parts.append(message.content)
            else:
                request_messages.append(_message_to_dict(message))

        request: dict[str, Any] = {
            "model": self.default_model,
            "max_tokens": (
                max_tokens if max_tokens is not None else self.default_max_tokens
            ),
            "messages": request_messages,
        }
        if system_parts:
            request["system"] = "\n\n".join(system_parts)
        if tools is not None:
            request["tools"] = [dict(tool) for tool in tools]
        if temperature is not None:
            request["temperature"] = temperature
        if self.default_thinking is not None:
            request["thinking"] = dict(self.default_thinking)
        return request


def _message_to_dict(message: BaseMessage) -> dict[str, Any]:
    if isinstance(message, HumanMessage):
        return {"role": "user", "content": message.content}

    if isinstance(message, AIMessage):
        if not message.tool_calls:
            return {"role": "assistant", "content": message.content}

        content: list[dict[str, Any]] = []
        if message.content:
            content.append({"type": "text", "text": message.content})
        content.extend(
            {
                "type": "tool_use",
                "id": tool_call.id,
                "name": tool_call.name,
                "input": dict(tool_call.arguments),
            }
            for tool_call in message.tool_calls
        )
        return {"role": "assistant", "content": content}

    if isinstance(message, ToolMessage):
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content,
                }
            ],
        }

    raise ProviderError(f"Unsupported message type: {type(message).__name__}")


def _response_from_message(response: Any) -> LLMResponse:
    text_parts: list[str] = []
    tool_calls: list[ToolCallRequest] = []
    for block in getattr(response, "content", None) or ():
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text_parts.append(getattr(block, "text", ""))
        elif block_type == "tool_use":
            arguments = getattr(block, "input", {}) or {}
            if not isinstance(arguments, Mapping):
                raise ProviderError("Tool use input must be an object")
            tool_calls.append(
                ToolCallRequest(
                    id=block.id,
                    name=block.name,
                    arguments=dict(arguments),
                )
            )

    usage = getattr(response, "usage", None)
    token_usage = _usage_from_message(usage)
    return LLMResponse(
        content="".join(text_parts) or None,
        tool_calls=tuple(tool_calls),
        finish_reason=getattr(response, "stop_reason", None),
        usage=token_usage,
    )


def _usage_from_message(usage: Any) -> TokenUsage | None:
    if usage is None:
        return None
    prompt_tokens = getattr(usage, "input_tokens", None)
    completion_tokens = getattr(usage, "output_tokens", None)
    if prompt_tokens is None or completion_tokens is None:
        return None
    return TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
