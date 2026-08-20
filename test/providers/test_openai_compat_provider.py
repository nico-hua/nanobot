import json
import unittest
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

from nanobot.providers import (
    AIMessage,
    HumanMessage,
    LLMResponse,
    OpenAICompatProvider,
    ProviderError,
    SystemMessage,
    TokenUsage,
    ToolCallRequest,
    ToolMessage,
)


class FakeStream:
    def __init__(self, chunks: Sequence[Any]) -> None:
        self._chunks = iter(chunks)

    def __aiter__(self) -> "FakeStream":
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._chunks)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class FakeCompletions:
    def __init__(self, response: Any, stream_chunks: Sequence[Any] = ()) -> None:
        self.response = response
        self.stream_chunks = stream_chunks
        self.requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> Any:
        self.requests.append(request)
        if request.get("stream"):
            return FakeStream(self.stream_chunks)
        return self.response


class FakeClient:
    def __init__(self, response: Any, stream_chunks: Sequence[Any] = ()) -> None:
        completions = FakeCompletions(response, stream_chunks)
        self.completions = completions
        self.chat = SimpleNamespace(completions=completions)


def completion_response(
    content: str | None = "hello",
    tool_calls: Sequence[Any] = (),
) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=4,
            completion_tokens=2,
            total_tokens=6,
        ),
    )


class OpenAICompatProviderTest(unittest.IsolatedAsyncioTestCase):
    async def test_complete_builds_chat_completion_request(self) -> None:
        response = completion_response()
        client = FakeClient(response)
        provider = OpenAICompatProvider(
            api_key="test-key",
            api_base="https://example.test/v1",
            default_model="test-model",
            client=client,
        )
        tool_call = ToolCallRequest(
            id="call-1",
            name="get_weather",
            arguments={"city": "Beijing"},
        )
        tools = (
            {
                "type": "function",
                "function": {"name": "get_weather"},
            },
        )

        result = await provider.complete(
            messages=(
                SystemMessage(content="You are helpful."),
                HumanMessage(content="What is the weather?"),
                AIMessage(content="", tool_calls=(tool_call,)),
                ToolMessage(content='{"temperature": 20}', tool_call_id="call-1"),
            ),
            tools=tools,
            max_tokens=32,
            temperature=0.1,
        )

        request = client.completions.requests[0]
        self.assertEqual(request["model"], "test-model")
        self.assertEqual(request["tools"], list(tools))
        self.assertEqual(request["max_tokens"], 32)
        self.assertEqual(request["temperature"], 0.1)
        self.assertEqual(request["messages"][0], {"role": "system", "content": "You are helpful."})
        self.assertEqual(request["messages"][1], {"role": "user", "content": "What is the weather?"})
        self.assertEqual(request["messages"][3]["tool_call_id"], "call-1")
        self.assertEqual(
            json.loads(request["messages"][2]["tool_calls"][0]["function"]["arguments"]),
            {"city": "Beijing"},
        )
        self.assertEqual(
            result,
            LLMResponse(
                content="hello",
                finish_reason="stop",
                usage=TokenUsage(prompt_tokens=4, completion_tokens=2, total_tokens=6),
            ),
        )

    async def test_complete_parses_tool_calls_and_usage(self) -> None:
        tool_call = SimpleNamespace(
            id="call-1",
            function=SimpleNamespace(
                name="get_weather",
                arguments='{"city": "Beijing"}',
            ),
        )
        client = FakeClient(completion_response(content=None, tool_calls=(tool_call,)))
        provider = OpenAICompatProvider("test-key", "https://example.test/v1", "test-model", client=client)

        result = await provider.complete((HumanMessage(content="Check the weather"),))

        self.assertEqual(result.content, None)
        self.assertEqual(result.tool_calls[0].id, "call-1")
        self.assertEqual(result.tool_calls[0].name, "get_weather")
        self.assertEqual(result.tool_calls[0].arguments, {"city": "Beijing"})
        self.assertEqual(result.usage, TokenUsage(prompt_tokens=4, completion_tokens=2, total_tokens=6))

    async def test_stream_emits_text_deltas_and_accumulates_tool_calls(self) -> None:
        chunks = (
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="Hel", tool_calls=None),
                        finish_reason=None,
                    )
                ],
                usage=None,
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content="lo",
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="call-1",
                                    function=SimpleNamespace(
                                        name="get_weather",
                                        arguments='{"city"',
                                    ),
                                )
                            ],
                        ),
                        finish_reason=None,
                    )
                ],
                usage=None,
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id=None,
                                    function=SimpleNamespace(
                                        name=None,
                                        arguments=': "Beijing"}',
                                    ),
                                )
                            ],
                        ),
                        finish_reason="tool_calls",
                    )
                ],
                usage=None,
            ),
            SimpleNamespace(
                choices=[],
                usage=SimpleNamespace(
                    prompt_tokens=5,
                    completion_tokens=3,
                    total_tokens=8,
                ),
            ),
        )
        client = FakeClient(completion_response(), chunks)
        provider = OpenAICompatProvider("test-key", "https://example.test/v1", "test-model", client=client)
        deltas: list[str] = []

        async def on_delta(delta: str) -> None:
            deltas.append(delta)

        result = await provider.stream(
            (HumanMessage(content="Check the weather"),),
            on_delta=on_delta,
        )

        self.assertEqual(deltas, ["Hel", "lo"])
        self.assertEqual(result.content, "Hello")
        self.assertEqual(result.finish_reason, "tool_calls")
        self.assertEqual(result.tool_calls[0].arguments, {"city": "Beijing"})
        self.assertEqual(result.usage, TokenUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8))

    async def test_provider_error_wraps_client_failure(self) -> None:
        class FailingCompletions:
            async def create(self, **request: Any) -> Any:
                raise RuntimeError("network failure")

        client = SimpleNamespace(chat=SimpleNamespace(completions=FailingCompletions()))
        provider = OpenAICompatProvider("test-key", "https://example.test/v1", "test-model", client=client)

        with self.assertRaises(ProviderError):
            await provider.complete((HumanMessage(content="hello"),))
