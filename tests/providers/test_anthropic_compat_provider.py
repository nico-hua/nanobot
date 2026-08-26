import unittest
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

from typing_extensions import Self

from nanobot.providers import (
    AIMessage,
    AnthropicCompatProvider,
    HumanMessage,
    LLMResponse,
    ProviderError,
    SystemMessage,
    TokenUsage,
    ToolCallRequest,
    ToolMessage,
)
from tests.tools.fakes import WeatherTool


class FakeTextStream:
    def __init__(self, deltas: Sequence[str]) -> None:
        self._deltas = iter(deltas)

    def __aiter__(self) -> "FakeTextStream":
        return self

    async def __anext__(self) -> str:
        try:
            return next(self._deltas)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class FakeMessageStream:
    def __init__(self, deltas: Sequence[str], response: Any) -> None:
        self.text_stream = FakeTextStream(deltas)
        self.response = response

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:  # noqa: PYI036
        return None

    async def get_final_message(self) -> Any:
        return self.response


class FakeMessages:
    def __init__(
        self,
        response: Any,
        stream_deltas: Sequence[str] = (),
    ) -> None:
        self.response = response
        self.stream_deltas = stream_deltas
        self.create_requests: list[dict[str, Any]] = []
        self.stream_requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> Any:
        self.create_requests.append(request)
        return self.response

    def stream(self, **request: Any) -> FakeMessageStream:
        self.stream_requests.append(request)
        return FakeMessageStream(self.stream_deltas, self.response)


class FakeClient:
    def __init__(self, response: Any, stream_deltas: Sequence[str] = ()) -> None:
        self.messages = FakeMessages(response, stream_deltas)


def message_response(content: Sequence[Any], stop_reason: str = "end_turn") -> Any:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=5, output_tokens=3),
    )


class AnthropicCompatProviderTest(unittest.IsolatedAsyncioTestCase):
    async def test_complete_builds_anthropic_messages_request(self) -> None:
        response = message_response((SimpleNamespace(type="text", text="hello"),))
        client = FakeClient(response)
        provider = AnthropicCompatProvider(
            api_key="test-key",
            api_base="https://example.test/anthropic",
            default_model="test-model",
            default_max_tokens=128,
            client=client,
        )
        tool_call = ToolCallRequest(
            id="call-1",
            name="get_weather",
            arguments={"city": "Beijing"},
        )
        tools = (WeatherTool(),)

        result = await provider.complete(
            (
                SystemMessage(content="You are helpful."),
                HumanMessage(content="What is the weather?"),
                AIMessage(content="I will check.", tool_calls=(tool_call,)),
                ToolMessage(content='{"temperature": 20}', tool_call_id="call-1"),
            ),
            tools=tools,
            max_tokens=32,
            temperature=0.1,
        )

        request = client.messages.create_requests[0]
        self.assertEqual(request["model"], "test-model")
        self.assertEqual(request["system"], "You are helpful.")
        self.assertEqual(
            request["tools"],
            [tool.to_anthropic_tool() for tool in tools],
        )
        self.assertEqual(request["max_tokens"], 32)
        self.assertEqual(request["temperature"], 0.1)
        self.assertEqual(request["messages"][0], {"role": "user", "content": "What is the weather?"})
        self.assertEqual(request["messages"][2]["role"], "user")
        self.assertEqual(request["messages"][2]["content"][0]["tool_use_id"], "call-1")
        self.assertEqual(result, LLMResponse(content="hello", finish_reason="end_turn", usage=TokenUsage(5, 3, 8)))

    async def test_complete_parses_text_tool_use_and_usage(self) -> None:
        response = message_response(
            (
                SimpleNamespace(type="text", text="I will check."),
                SimpleNamespace(
                    type="tool_use",
                    id="call-1",
                    name="get_weather",
                    input={"city": "Beijing"},
                ),
            ),
            stop_reason="tool_use",
        )
        client = FakeClient(response)
        provider = AnthropicCompatProvider("test-key", "https://example.test/anthropic", "test-model", client=client)

        result = await provider.complete((HumanMessage(content="Check the weather"),))

        self.assertEqual(result.content, "I will check.")
        self.assertEqual(result.tool_calls, (ToolCallRequest("call-1", "get_weather", {"city": "Beijing"}),))
        self.assertEqual(result.finish_reason, "tool_use")
        self.assertEqual(result.usage, TokenUsage(5, 3, 8))

    async def test_stream_emits_deltas_and_returns_final_response(self) -> None:
        response = message_response((SimpleNamespace(type="text", text="Hello"),))
        client = FakeClient(response, ("Hel", "lo"))
        provider = AnthropicCompatProvider("test-key", "https://example.test/anthropic", "test-model", client=client)
        deltas: list[str] = []
        tools = (WeatherTool(),)

        async def on_delta(delta: str) -> None:
            deltas.append(delta)

        result = await provider.stream(
            (HumanMessage(content="Say hello"),),
            tools=tools,
            on_delta=on_delta,
        )

        self.assertEqual(
            client.messages.stream_requests[0]["tools"],
            [tool.to_anthropic_tool() for tool in tools],
        )
        self.assertEqual(deltas, ["Hel", "lo"])
        self.assertEqual(result.content, "Hello")
        self.assertEqual(client.messages.stream_requests[0]["max_tokens"], 1024)

    async def test_provider_error_wraps_client_failure(self) -> None:
        class FailingMessages:
            async def create(self, **request: Any) -> Any:
                raise RuntimeError("network failure")

        client = SimpleNamespace(messages=FailingMessages())
        provider = AnthropicCompatProvider("test-key", "https://example.test/anthropic", "test-model", client=client)

        with self.assertRaises(ProviderError):
            await provider.complete((HumanMessage(content="hello"),))
