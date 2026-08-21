import unittest
from collections.abc import Awaitable, Callable, Sequence

from nanobot.providers import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    LLMProvider,
    LLMResponse,
    Message,
    ProviderError,
    SystemMessage,
    TokenUsage,
    ToolCallRequest,
    ToolMessage,
)
from nanobot.tools import Tool
from test.tools.fakes import WeatherTool


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

    def test_llm_response_contains_text_tool_calls_finish_reason_and_usage(self) -> None:
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
