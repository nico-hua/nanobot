"""Vendor-neutral interfaces and data models for LLM providers."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from ..tools import Tool
from .messages import BaseMessage, ToolCallRequest

logger = logging.getLogger(__name__)

_DEFAULT_PROVIDER_ERROR = "LLM provider request failed."
_MISSING_PROVIDER_ERROR = "LLM provider reported an error."


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
    error: str | None = None

    def __post_init__(self) -> None:
        """Keep every normalized provider failure explicit and consistent."""

        if self.error is not None and not isinstance(self.error, str):
            raise TypeError("LLMResponse error must be a string or None")
        error = self.error.strip() if self.error is not None else None
        if self.finish_reason == "error" and not error:
            error = _MISSING_PROVIDER_ERROR
        finish_reason = "error" if error else self.finish_reason
        object.__setattr__(self, "error", error)
        object.__setattr__(self, "finish_reason", finish_reason)


class ProviderError(Exception):
    """Internal failure raised while normalizing one provider request."""


class ProviderTransientError(ProviderError):
    """A provider failure explicitly marked as safe to retry."""


class ProviderTimeoutError(ProviderTransientError):
    """Internal failure for one provider request exceeding its timeout."""


class ProviderCallbackError(ProviderError):
    """Raised when forwarding provider output to the caller fails."""


async def run_provider_request(
    request: Callable[[], Awaitable[LLMResponse]],
    *,
    timeout_seconds: float,
    max_retries: int,
    can_retry: Callable[[Exception], bool] | None = None,
) -> LLMResponse:
    """Run one provider operation with a bounded retry policy.

    ``request`` must construct a fresh awaitable for every attempt.  Failures
    are normalized into ``LLMResponse(error=...)`` so callers can retain a
    single result path.  Cancellation remains a control-flow signal.
    """

    _validate_request_settings(timeout_seconds, max_retries)
    retry_predicate = can_retry or is_transient_provider_error
    for attempt in range(max_retries + 1):
        try:
            response = await asyncio.wait_for(
                request(),
                timeout=timeout_seconds,
            )
            return response
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            failure: Exception = ProviderTimeoutError(
                f"LLM request timed out after {timeout_seconds:g} seconds"
            )
        except Exception as error:  # noqa: BLE001
            failure = error

        if attempt < max_retries and retry_predicate(failure):
            delay_seconds = attempt + 1
            logger.warning(
                "Retrying transient provider request (attempt=%d, max_retries=%d, error_type=%s)",
                attempt + 1,
                max_retries,
                type(failure).__name__,
            )
            await asyncio.sleep(delay_seconds)
            continue

        logger.warning(
            "Provider request failed (attempt=%d, error_type=%s)",
            attempt + 1,
            type(failure).__name__,
        )
        return LLMResponse(error=_provider_error_message(failure, timeout_seconds))

    raise AssertionError("provider retry loop did not return a response")


def is_transient_provider_error(error: Exception) -> bool:
    """Return whether an SDK failure is safe to retry without request context."""

    if isinstance(error, ProviderCallbackError):
        return False
    if isinstance(error, ProviderTransientError):
        return True
    if isinstance(error, (ConnectionError, OSError)):
        return True

    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int):
        return status_code == 429 or 500 <= status_code <= 599

    if getattr(error, "transient", False) is True:
        return True
    return type(error).__name__ in {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "TimeoutException",
    }


def _validate_request_settings(timeout_seconds: float, max_retries: int) -> None:
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be positive")
    if not isinstance(max_retries, int) or isinstance(max_retries, bool):
        raise TypeError("max_retries must be an integer")
    if max_retries < 0:
        raise ValueError("max_retries must not be negative")


def _provider_error_message(error: Exception, timeout_seconds: float) -> str:
    """Map provider failures to safe, user-facing messages without SDK details."""

    if isinstance(error, ProviderTimeoutError) or type(error).__name__ in {
        "APITimeoutError",
        "ConnectTimeout",
        "ReadTimeout",
        "TimeoutException",
    }:
        return f"LLM request timed out after {timeout_seconds:g} seconds."
    if isinstance(error, ProviderCallbackError):
        return "LLM streaming output could not be delivered."

    status_code = getattr(error, "status_code", None)
    if status_code == 429:
        return "LLM provider rate limit exceeded."
    if isinstance(status_code, int) and 500 <= status_code <= 599:
        return "LLM provider service is temporarily unavailable."
    if status_code == 401:
        return "LLM provider authentication failed."
    if status_code == 403:
        return "LLM provider permission was denied."
    if isinstance(status_code, int) and 400 <= status_code <= 499:
        return "LLM provider rejected the request."
    if isinstance(error, (ConnectionError, OSError)) or type(error).__name__ in {
        "APIConnectionError",
        "ConnectError",
    }:
        return "LLM provider connection failed."
    if isinstance(error, ProviderError):
        return "LLM provider returned an invalid response."
    return _DEFAULT_PROVIDER_ERROR


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
        """Complete a conversation and return a normalized response or error."""

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
        """Stream text deltas and return the final normalized response or error."""

        raise NotImplementedError
