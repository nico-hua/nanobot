"""OpenAI-compatible implementation of the LLM provider interface."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from openai import AsyncOpenAI

from ..tools import Tool
from .base import (
    LLMProvider,
    LLMResponse,
    ProviderCallbackError,
    ProviderError,
    TokenUsage,
    is_transient_provider_error,
    run_provider_request,
)
from .messages import AIMessage, BaseMessage, ToolCallRequest, ToolMessage

logger = logging.getLogger(__name__)


class OpenAICompatProvider(LLMProvider):
    """Call chat-completions APIs that implement the OpenAI protocol."""

    def __init__(
        self,
        api_key: str,
        api_base: str,
        default_model: str,
        default_max_tokens: int | None = None,
        default_temperature: float | None = None,
        request_timeout_seconds: float = 60.0,
        max_retries: int = 2,
        *,
        client: Any | None = None,
    ) -> None:
        self.api_base = api_base
        self.default_model = default_model
        self.default_max_tokens = default_max_tokens
        self.default_temperature = default_temperature
        self._request_timeout_seconds = request_timeout_seconds
        self._max_retries = max_retries
        self._client = client if client is not None else AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            # SDK retries would make the configured request count ambiguous;
            # all retries go through ``run_provider_request`` instead.
            max_retries=0,
        )

    async def complete(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        request = self._build_request(messages, tools, max_tokens, temperature)
        logger.debug(
            "OpenAI-compatible completion requested (model=%s, messages=%d, tools=%d)",
            self.default_model,
            len(messages),
            len(tools or ()),
        )

        async def request_once() -> LLMResponse:
            response = await self._client.chat.completions.create(**request)
            return _response_from_completion(response)

        return await run_provider_request(
            request_once,
            timeout_seconds=self._request_timeout_seconds,
            max_retries=self._max_retries,
        )

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        request = self._build_request(messages, tools, max_tokens, temperature)
        request["stream"] = True
        logger.debug(
            "OpenAI-compatible streaming requested (model=%s, messages=%d, tools=%d)",
            self.default_model,
            len(messages),
            len(tools or ()),
        )

        output_started = False

        async def consume_stream() -> LLMResponse:
            nonlocal output_started
            response = await self._client.chat.completions.create(**request)
            content_parts: list[str] = []
            tool_call_parts: dict[int, dict[str, str]] = {}
            finish_reason: str | None = None
            usage: TokenUsage | None = None

            async for chunk in response:
                usage = _usage_from_completion(getattr(chunk, "usage", None)) or usage
                choices = getattr(chunk, "choices", None) or ()
                if not choices:
                    continue

                choice = choices[0]
                finish_reason = getattr(choice, "finish_reason", None) or finish_reason
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue

                content = getattr(delta, "content", None)
                if content:
                    content_parts.append(content)
                    if on_delta is not None:
                        # Treat a failed presentation callback as terminal,
                        # not as a transient model transport failure.
                        output_started = True
                        try:
                            await on_delta(content)
                        except asyncio.CancelledError:
                            raise
                        except Exception as error:
                            raise ProviderCallbackError(
                                "LLM stream callback failed"
                            ) from error

                for tool_call in getattr(delta, "tool_calls", None) or ():
                    _append_tool_call(tool_call_parts, tool_call)

            tool_calls = tuple(
                _tool_call_from_parts(tool_call_parts[index])
                for index in sorted(tool_call_parts)
            )
            return LLMResponse(
                content="".join(content_parts) or None,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                usage=usage,
            )

        return await run_provider_request(
            consume_stream,
            timeout_seconds=self._request_timeout_seconds,
            max_retries=self._max_retries,
            can_retry=lambda error: (
                not output_started and is_transient_provider_error(error)
            ),
        )

    def _build_request(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None,
        max_tokens: int | None,
        temperature: float | None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.default_model,
            "messages": [_message_to_dict(message) for message in messages],
        }
        if tools is not None:
            request["tools"] = [tool.to_openai_tool() for tool in tools]
        resolved_max_tokens = (
            max_tokens if max_tokens is not None else self.default_max_tokens
        )
        if resolved_max_tokens is not None:
            request["max_tokens"] = resolved_max_tokens
        resolved_temperature = (
            temperature if temperature is not None else self.default_temperature
        )
        if resolved_temperature is not None:
            request["temperature"] = resolved_temperature
        return request


def _message_to_dict(message: BaseMessage) -> dict[str, Any]:
    result: dict[str, Any] = {
        "role": message.role,
        "content": message.content,
    }
    if isinstance(message, AIMessage) and message.tool_calls:
        result["tool_calls"] = [
            {
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.name,
                    "arguments": json.dumps(tool_call.arguments),
                },
            }
            for tool_call in message.tool_calls
        ]
    elif isinstance(message, ToolMessage):
        result["tool_call_id"] = message.tool_call_id
    return result


def _response_from_completion(response: Any) -> LLMResponse:
    choices = getattr(response, "choices", None) or ()
    if not choices:
        raise ProviderError("OpenAI-compatible response contained no choices")

    choice = choices[0]
    message = choice.message
    tool_calls = tuple(
        _tool_call_from_response(tool_call)
        for tool_call in (getattr(message, "tool_calls", None) or ())
    )
    return LLMResponse(
        content=getattr(message, "content", None),
        tool_calls=tool_calls,
        finish_reason=getattr(choice, "finish_reason", None),
        usage=_usage_from_completion(getattr(response, "usage", None)),
    )


def _tool_call_from_response(tool_call: Any) -> ToolCallRequest:
    function = tool_call.function
    return _tool_call_from_parts(
        {
            "id": tool_call.id,
            "name": function.name,
            "arguments": function.arguments or "{}",
        }
    )


def _append_tool_call(
    tool_call_parts: dict[int, dict[str, str]],
    tool_call: Any,
) -> None:
    index = getattr(tool_call, "index", None)
    if index is None:
        index = len(tool_call_parts)
    parts = tool_call_parts.setdefault(
        index,
        {"id": "", "name": "", "arguments": ""},
    )
    parts["id"] += getattr(tool_call, "id", None) or ""
    function = getattr(tool_call, "function", None)
    if function is not None:
        parts["name"] += getattr(function, "name", None) or ""
        parts["arguments"] += getattr(function, "arguments", None) or ""


def _tool_call_from_parts(parts: Mapping[str, str]) -> ToolCallRequest:
    try:
        arguments = json.loads(parts["arguments"] or "{}")
    except json.JSONDecodeError as exc:
        raise ProviderError("Tool call arguments were not valid JSON") from exc
    if not isinstance(arguments, dict):
        raise ProviderError("Tool call arguments must be a JSON object")
    return ToolCallRequest(
        id=parts["id"],
        name=parts["name"],
        arguments=arguments,
    )


def _usage_from_completion(usage: Any) -> TokenUsage | None:
    if usage is None:
        return None
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    total_tokens = getattr(usage, "total_tokens", None)
    if prompt_tokens is None or completion_tokens is None or total_tokens is None:
        return None
    return TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )
