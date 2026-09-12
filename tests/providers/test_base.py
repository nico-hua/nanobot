import asyncio
import unittest
from collections.abc import Awaitable, Callable, Sequence
from unittest.mock import patch

from nanobot.providers import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    LLMProvider,
    LLMResponse,
    Message,
    ProviderError,
    ProviderTransientError,
    SystemMessage,
    TokenUsage,
    ToolCallRequest,
    ToolMessage,
)
from nanobot.providers.base import run_provider_request
from nanobot.tools import Tool
from tests.tools.fakes import WeatherTool


class FakeProvider(LLMProvider):
    def __init__(self) -> None:
        self.received_messages: tuple[BaseMessage, ...] | None = None
        self.received_options: tuple[
            Sequence[Tool] | None,
            int | None,
            float | None,
        ] | None = None

    async def complete(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        self.received_messages = tuple(messages)
        self.received_options = (tools, max_tokens, temperature)
        return LLMResponse(content="done")

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        self.received_messages = tuple(messages)
        self.received_options = (tools, max_tokens, temperature)
        for delta in ("hel", "lo"):
            if on_delta is not None:
                await on_delta(delta)
        return LLMResponse(content="hello", finish_reason="stop")


class ProviderModelsTest(unittest.TestCase):
    def test_message_classes_have_expected_roles(self) -> None:
        tool_call = ToolCallRequest(
            id="call-1",
            name="search",
            arguments={"query": "weather"},
        )
        messages = (
            SystemMessage(content="You are helpful."),
            HumanMessage(content="What is the weather?"),
            AIMessage(content="I will check.", tool_calls=(tool_call,)),
            ToolMessage(content='{"temperature": 20}', tool_call_id="call-1"),
        )

        self.assertEqual(
            [message.role for message in messages],
            ["system", "user", "assistant", "tool"],
        )
        self.assertIsInstance(messages[1], Message)
        self.assertEqual(messages[2].tool_calls, (tool_call,))
        self.assertEqual(messages[3].tool_call_id, "call-1")

    def test_tool_message_keeps_tool_call_id(self) -> None:
        message = ToolMessage(
            content='{"result": "sunny"}',
            tool_call_id="call-1",
        )

        self.assertEqual(message.role, "tool")
        self.assertEqual(message.content, '{"result": "sunny"}')
        self.assertEqual(message.tool_call_id, "call-1")

    def test_llm_response_contains_text_tool_calls_finish_reason_usage_and_error(self) -> None:
        tool_call = ToolCallRequest(
            id="call-1",
            name="search",
            arguments={"query": "python"},
        )
        usage = TokenUsage(
            prompt_tokens=12,
            completion_tokens=8,
            total_tokens=20,
        )

        response = LLMResponse(
            content="I found a result.",
            tool_calls=(tool_call,),
            finish_reason="stop",
            usage=usage,
        )

        self.assertEqual(response.content, "I found a result.")
        self.assertEqual(response.tool_calls, (tool_call,))
        self.assertEqual(response.finish_reason, "stop")
        self.assertEqual(response.usage, usage)
        self.assertIsNone(response.error)

    def test_llm_response_normalizes_missing_error_details(self) -> None:
        response = LLMResponse(finish_reason="error")
        explicit_error = LLMResponse(content="ignored", error=" request failed ")

        self.assertEqual(response.error, "LLM provider reported an error.")
        self.assertEqual(response.finish_reason, "error")
        self.assertEqual(explicit_error.error, "request failed")
        self.assertEqual(explicit_error.finish_reason, "error")

    def test_tool_call_request_keeps_id_name_and_arguments(self) -> None:
        request = ToolCallRequest(
            id="call-1",
            name="search",
            arguments={"query": "python"},
        )

        self.assertEqual(request.id, "call-1")
        self.assertEqual(request.name, "search")
        self.assertEqual(request.arguments, {"query": "python"})

    def test_provider_is_abstract(self) -> None:
        with self.assertRaises(TypeError):
            LLMProvider()

    def test_provider_error_is_an_exception(self) -> None:
        error = ProviderError("request failed")

        self.assertIsInstance(error, Exception)
        self.assertEqual(str(error), "request failed")


class LLMProviderTest(unittest.IsolatedAsyncioTestCase):
    async def test_complete_receives_messages_and_returns_response(self) -> None:
        provider = FakeProvider()
        messages = (
            SystemMessage(content="You are helpful."),
            HumanMessage(content="Hello"),
        )

        response = await provider.complete(messages)

        self.assertEqual(provider.received_messages, messages)
        self.assertEqual(response, LLMResponse(content="done"))

    async def test_complete_accepts_tools_and_generation_options(self) -> None:
        provider = FakeProvider()
        messages = (HumanMessage(content="Search for Python"),)
        tools = (WeatherTool(),)

        response = await provider.complete(
            messages,
            tools=tools,
            max_tokens=128,
            temperature=0.2,
        )

        self.assertEqual(provider.received_messages, messages)
        self.assertEqual(provider.received_options, (tools, 128, 0.2))
        self.assertEqual(response, LLMResponse(content="done"))

    async def test_stream_emits_deltas_and_returns_final_response(self) -> None:
        provider = FakeProvider()
        messages = (HumanMessage(content="Say hello"),)
        tools = (WeatherTool(),)
        deltas: list[str] = []

        async def on_delta(delta: str) -> None:
            deltas.append(delta)

        response = await provider.stream(
            messages,
            tools=tools,
            max_tokens=64,
            temperature=0.0,
            on_delta=on_delta,
        )

        self.assertEqual(provider.received_messages, messages)
        self.assertEqual(provider.received_options, (tools, 64, 0.0))
        self.assertEqual(deltas, ["hel", "lo"])
        self.assertEqual(response, LLMResponse(content="hello", finish_reason="stop"))


class ProviderRequestRetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_normal_response_does_not_retry(self) -> None:
        attempts = 0

        async def request() -> LLMResponse:
            nonlocal attempts
            attempts += 1
            return LLMResponse(content="done")

        response = await run_provider_request(
            request,
            timeout_seconds=1,
            max_retries=2,
        )

        self.assertEqual(response, LLMResponse(content="done"))
        self.assertEqual(attempts, 1)

    async def test_timeout_retries_with_incrementing_delays(self) -> None:
        attempts = 0
        delays: list[float] = []

        async def request() -> LLMResponse:
            nonlocal attempts
            attempts += 1
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def record_delay(delay: float) -> None:
            delays.append(delay)

        with patch("nanobot.providers.base.asyncio.sleep", new=record_delay):
            response = await run_provider_request(
                request,
                timeout_seconds=0.01,
                max_retries=2,
            )

        self.assertEqual(attempts, 3)
        self.assertEqual(delays, [1, 2])
        self.assertEqual(response.finish_reason, "error")
        self.assertEqual(response.error, "LLM request timed out after 0.01 seconds.")

    async def test_connection_rate_limit_server_and_marked_transient_failures_retry(self) -> None:
        class StatusError(Exception):
            def __init__(self, status_code: int) -> None:
                self.status_code = status_code

        for failure in (
            ConnectionError(),
            StatusError(429),
            StatusError(503),
            ProviderTransientError("temporary"),
        ):
            with self.subTest(failure=type(failure).__name__):
                attempts = 0
                delays: list[float] = []

                async def request() -> LLMResponse:
                    nonlocal attempts
                    attempts += 1
                    if attempts == 1:
                        raise failure
                    return LLMResponse(content="recovered")

                async def record_delay(delay: float) -> None:
                    delays.append(delay)

                with patch("nanobot.providers.base.asyncio.sleep", new=record_delay):
                    response = await run_provider_request(
                        request,
                        timeout_seconds=1,
                        max_retries=2,
                    )

                self.assertEqual(response.content, "recovered")
                self.assertIsNone(response.error)
                self.assertEqual(attempts, 2)
                self.assertEqual(delays, [1])

    async def test_non_transient_failures_do_not_retry(self) -> None:
        class StatusError(Exception):
            def __init__(self, status_code: int) -> None:
                self.status_code = status_code

        for failure in (
            StatusError(401),
            StatusError(400),
            StatusError(413),
            ProviderError("invalid tool schema"),
        ):
            with self.subTest(failure=type(failure).__name__):
                attempts = 0

                async def request() -> LLMResponse:
                    nonlocal attempts
                    attempts += 1
                    raise failure

                response = await run_provider_request(
                    request,
                    timeout_seconds=1,
                    max_retries=2,
                )

                self.assertEqual(attempts, 1)
                self.assertEqual(response.finish_reason, "error")
                self.assertIsNotNone(response.error)

    async def test_zero_max_retries_keeps_a_transient_request_single_shot(self) -> None:
        attempts = 0

        async def request() -> LLMResponse:
            nonlocal attempts
            attempts += 1
            raise ConnectionError()

        response = await run_provider_request(
            request,
            timeout_seconds=1,
            max_retries=0,
        )

        self.assertEqual(attempts, 1)
        self.assertEqual(response.error, "LLM provider connection failed.")

    async def test_retry_exhaustion_returns_one_error_response(self) -> None:
        attempts = 0
        delays: list[float] = []

        async def request() -> LLMResponse:
            nonlocal attempts
            attempts += 1
            raise ConnectionError()

        async def record_delay(delay: float) -> None:
            delays.append(delay)

        with patch("nanobot.providers.base.asyncio.sleep", new=record_delay):
            response = await run_provider_request(
                request,
                timeout_seconds=1,
                max_retries=2,
            )

        self.assertEqual(attempts, 3)
        self.assertEqual(delays, [1, 2])
        self.assertEqual(response.error, "LLM provider connection failed.")
        self.assertEqual(response.finish_reason, "error")

    async def test_ordinary_exception_becomes_a_safe_error_response(self) -> None:
        async def request() -> LLMResponse:
            raise RuntimeError("do not expose request headers or secrets")

        response = await run_provider_request(
            request,
            timeout_seconds=1,
            max_retries=0,
        )

        self.assertEqual(response.finish_reason, "error")
        self.assertEqual(response.error, "LLM provider request failed.")

    async def test_cancellation_is_not_converted_to_an_error_response(self) -> None:
        started = asyncio.Event()

        async def request() -> LLMResponse:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        task = asyncio.create_task(
            run_provider_request(
                request,
                timeout_seconds=1,
                max_retries=2,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task
