"""Vendor-neutral interfaces and data models for LLM providers."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from ..tools import Tool
from .messages import BaseMessage, ToolCallRequest


@dataclass(frozen=True)
class TokenUsage:
    """Token counts reported by a provider."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class LLMResponse:
    """The normalized result of one LLM completion."""

    content: str | None = None
    tool_calls: tuple[ToolCallRequest, ...] = ()
    finish_reason: str | None = None
    usage: TokenUsage | None = None


class ProviderError(Exception):
    """Base error raised when an LLM provider cannot complete a request."""


class ProviderTimeoutError(ProviderError):
    """Raised when one LLM provider request exceeds its configured timeout."""


ResponseT = TypeVar("ResponseT")


async def await_provider_response(
    request: Awaitable[ResponseT],
    *,
    timeout_seconds: float,
) -> ResponseT:
    """Wait for one provider request without changing cancellation semantics."""

    try:
        return await asyncio.wait_for(request, timeout=timeout_seconds)
    except asyncio.CancelledError:
        raise
    except TimeoutError as error:
        raise ProviderTimeoutError(
            f"LLM request timed out after {timeout_seconds:g} seconds"
        ) from error


class LLMProvider(ABC):
    """Abstract interface implemented by concrete LLM providers."""

    @abstractmethod
    async def complete(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        """Complete a conversation and return a normalized response."""

        raise NotImplementedError

    @abstractmethod
    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """Stream text deltas and return the final normalized response."""

        raise NotImplementedError
