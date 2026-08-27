"""Focused tests for one-turn AgentLoop orchestration."""

from __future__ import annotations

import unittest
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from nanobot.agent import (
    AgentLoop,
    AgentRunner,
    AgentRunnerError,
    AgentRunResult,
    AgentRunSpec,
    SessionStore,
)
from nanobot.providers import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
    ToolMessage,
)
from nanobot.tools import Tool, ToolParameter, ToolRegistry, ToolResult


class ScriptedProvider(LLMProvider):
    def __init__(self, responses: Sequence[LLMResponse]) -> None:
        self._responses = iter(responses)
        self.complete_calls: list[tuple[BaseMessage, ...]] = []

    async def complete(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        del tools, max_tokens, temperature
        self.complete_calls.append(tuple(messages))
        return next(self._responses)

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        raise AssertionError("AgentLoop must not use streaming")


class EchoTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="echo",
            description="Echo a value.",
            parameters=(
                ToolParameter(
                    name="value",
                    description="The value to echo.",
                    type="string",
                    required=True,
                ),
            ),
        )

    async def execute(self, **arguments: Any) -> ToolResult:
        return ToolResult(content=f"echo: {arguments['value']}")


class FailingRunner(AgentRunner):
    def __init__(self) -> None:
        self.received_spec: AgentRunSpec | None = None

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.received_spec = spec
        raise AgentRunnerError("model unavailable")


class AgentLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_passes_the_user_message_to_the_runner(self) -> None:
        provider = ScriptedProvider((LLMResponse(content="Hello."),))
        session_store = SessionStore()
        loop = AgentLoop(AgentRunner(), provider, ToolRegistry(), session_store)

        result = await loop.process_direct("Hello", "test", "chat-1", "session-1")

        self.assertEqual(result.content, "Hello.")
        self.assertEqual(
            provider.complete_calls,
            [(HumanMessage(content="Hello"),)],
        )

    async def test_includes_saved_history_in_the_next_request(self) -> None:
        provider = ScriptedProvider(
            (
                LLMResponse(content="First answer."),
                LLMResponse(content="Second answer."),
            )
        )
        session_store = SessionStore()
        loop = AgentLoop(AgentRunner(), provider, ToolRegistry(), session_store)

        await loop.process_direct("First question.", "test", "chat-1", "session-1")
        await loop.process_direct("Second question.", "test", "chat-1", "session-1")

        self.assertEqual(
            provider.complete_calls[1],
            (
                HumanMessage(content="First question."),
                AIMessage(content="First answer."),
                HumanMessage(content="Second question."),
            ),
        )

    async def test_saves_complete_messages_including_tool_call_history(self) -> None:
        request = ToolCallRequest(
            id="call-1",
            name="echo",
            arguments={"value": "hello"},
        )
        provider = ScriptedProvider(
            (
                LLMResponse(content="I will echo it.", tool_calls=(request,)),
                LLMResponse(content="The value was echoed."),
            )
        )
        session_store = SessionStore()
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry((EchoTool(),)),
            session_store,
        )

        result = await loop.process_direct(
            "Echo hello.",
            "test",
            "chat-1",
            "session-1",
        )

        self.assertEqual(session_store.load("session-1"), result.messages)
        self.assertEqual(
            result.messages,
            (
                HumanMessage(content="Echo hello."),
                AIMessage(content="I will echo it.", tool_calls=(request,)),
                ToolMessage(content="echo: hello", tool_call_id="call-1"),
                AIMessage(content="The value was echoed."),
            ),
        )

    async def test_does_not_save_new_messages_when_the_runner_fails(self) -> None:
        previous_history = (HumanMessage(content="Previous question."),)
        session_store = SessionStore()
        session_store.save("session-1", previous_history)
        runner = FailingRunner()
        loop = AgentLoop(runner, ScriptedProvider(()), ToolRegistry(), session_store)

        with self.assertRaisesRegex(AgentRunnerError, "model unavailable"):
            await loop.process_direct(
                "New question.",
                "test",
                "chat-1",
                "session-1",
            )

        self.assertEqual(session_store.load("session-1"), previous_history)
        self.assertEqual(
            runner.received_spec.messages if runner.received_spec is not None else None,
            (*previous_history, HumanMessage(content="New question.")),
        )
