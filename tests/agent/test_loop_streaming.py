"""Focused streaming integration tests for ``AgentLoop``."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from nanobot.agent import AgentLoop, AgentRunner, ContextBuilder
from nanobot.bus import InboundMessage, MessageBus
from nanobot.providers import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    LLMProvider,
    LLMResponse,
    TokenUsage,
    ToolCallRequest,
    ToolMessage,
)
from nanobot.session import SessionManager
from nanobot.tools import Tool, ToolParameter, ToolRegistry, ToolResult


class RecordingProvider(LLMProvider):
    """Provider double that records the execution mode selected by AgentLoop."""

    def __init__(self) -> None:
        self.complete_calls = 0
        self.stream_calls = 0

    async def complete(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        del messages, tools, max_tokens, temperature
        self.complete_calls += 1
        return LLMResponse(content="Complete reply.", finish_reason="stop")

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        del messages, tools, max_tokens, temperature
        self.stream_calls += 1
        for chunk in ("First ", "second."):
            if on_delta is not None:
                await on_delta(chunk)
        return LLMResponse(
            content="First second.",
            finish_reason="stop",
            usage=TokenUsage(5, 2, 7),
        )


class ToolCallingStreamingProvider(LLMProvider):
    """Provider double that requests a tool before yielding final text."""

    def __init__(self, tool_call: ToolCallRequest) -> None:
        self._tool_call = tool_call
        self.stream_calls = 0

    async def complete(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        del messages, tools, max_tokens, temperature
        raise AssertionError("Tool streaming test must not use completion")

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        del messages, tools, max_tokens, temperature
        self.stream_calls += 1
        if self.stream_calls == 1:
            return LLMResponse(tool_calls=(self._tool_call,))
        if on_delta is not None:
            await on_delta("Done.")
        return LLMResponse(content="Done.", finish_reason="stop")


class EchoTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            "echo",
            "Echo a supplied value.",
            parameters=(
                ToolParameter(
                    name="value",
                    description="Value to echo.",
                    type="string",
                    required=True,
                ),
            ),
        )

    async def execute(self, **arguments: Any) -> ToolResult:
        return ToolResult(content=f"echo: {arguments['value']}")


class AgentLoopStreamingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_metadata_missing_uses_the_non_streaming_runner_path(self) -> None:
        provider = RecordingProvider()
        loop = self._loop(provider)

        response = await loop.process_inbound(self._inbound("Non-streaming request."))

        self.assertIsNotNone(response)
        self.assertEqual(response.content if response else None, "Complete reply.")
        self.assertEqual(provider.complete_calls, 1)
        self.assertEqual(provider.stream_calls, 0)

    async def test_streaming_publishes_ordered_deltas_without_a_final_duplicate(self) -> None:
        provider = RecordingProvider()
        bus = MessageBus()
        loop = self._loop(provider, message_bus=bus)
        inbound = self._inbound(
            "Streaming request.",
            metadata={"streaming": True, "origin": "test"},
        )

        response = await loop.process_inbound(inbound)

        first = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        second = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        turn_end = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        self.assertIsNone(response)
        self.assertEqual(provider.complete_calls, 0)
        self.assertEqual(provider.stream_calls, 1)
        self.assertEqual([first.content, second.content], ["First ", "second."])
        self.assertEqual(first.metadata, {"streaming": True, "origin": "test", "event": "delta"})
        self.assertEqual(second.metadata, first.metadata)
        self.assertEqual(turn_end.content, "First second.")
        self.assertEqual(
            turn_end.metadata,
            {
                "streaming": True,
                "origin": "test",
                "event": "turn_end",
                "tools_used": [],
                "token_usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 2,
                    "total_tokens": 7,
                },
                "stop_reason": "stop",
            },
        )
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(bus.consume_outbound(), timeout=0.05)

        session = loop._session_manager.get_or_create("session-1")
        self.assertEqual(
            session.messages,
            (
                HumanMessage(content="Streaming request."),
                AIMessage(content="First second."),
            ),
        )

    async def test_streaming_publishes_tool_call_before_text_and_turn_end(self) -> None:
        request = ToolCallRequest(
            id="call-1",
            name="echo",
            arguments={"value": "Beijing"},
        )
        provider = ToolCallingStreamingProvider(request)
        bus = MessageBus()
        loop = self._loop(provider, message_bus=bus, tools=(EchoTool(),))

        response = await loop.process_inbound(
            self._inbound("Call the tool.", metadata={"streaming": True})
        )

        tool_event = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        delta = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        turn_end = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        self.assertIsNone(response)
        self.assertEqual(tool_event.content, "")
        self.assertEqual(
            tool_event.metadata,
            {
                "streaming": True,
                "event": "tool_call",
                "tool_call": {
                    "id": "call-1",
                    "name": "echo",
                    "arguments": {"value": "Beijing"},
                },
            },
        )
        self.assertEqual(delta.metadata["event"], "delta")
        self.assertEqual(delta.content, "Done.")
        self.assertEqual(turn_end.metadata["event"], "turn_end")
        self.assertEqual(turn_end.content, "Done.")

        session = loop._session_manager.get_or_create("session-1")
        self.assertEqual(
            session.messages,
            (
                HumanMessage(content="Call the tool."),
                AIMessage(content="", tool_calls=(request,)),
                ToolMessage(content="echo: Beijing", tool_call_id="call-1"),
                AIMessage(content="Done."),
            ),
        )

    def _loop(
        self,
        provider: LLMProvider,
        *,
        message_bus: MessageBus | None = None,
        tools: Sequence[Tool] = (),
    ) -> AgentLoop:
        workspace = Path(self._temporary_directory.name) / "workspace"
        return AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(tools),
            SessionManager(workspace),
            ContextBuilder(workspace),
            message_bus=message_bus,
        )

    @staticmethod
    def _inbound(
        content: str,
        *,
        metadata: dict[str, object] | None = None,
    ) -> InboundMessage:
        return InboundMessage(
            channel="test",
            chat_id="chat-1",
            sender_id="sender-1",
            session_id="session-1",
            content=content,
            metadata=metadata or {},
        )
