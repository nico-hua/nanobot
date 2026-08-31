"""Integration tests for AgentLoop session persistence."""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from nanobot.agent import AgentLoop, AgentRunner, AgentRunnerError, AgentRunResult, AgentRunSpec
from nanobot.providers import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    LLMProvider,
    LLMResponse,
    SystemMessage,
    ToolCallRequest,
    ToolMessage,
)
from nanobot.session import SessionManager
from nanobot.tools import Tool, ToolParameter, ToolRegistry, ToolResult

_SYSTEM_MESSAGE = SystemMessage(content="你是一个有用的助手")


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
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._sessions = SessionManager(self._temporary_directory.name)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_continuous_messages_use_the_session_id_and_persist_the_system_prompt(self) -> None:
        provider = ScriptedProvider(
            (
                LLMResponse(content="First answer."),
                LLMResponse(content="Second answer."),
            )
        )
        loop = AgentLoop(AgentRunner(), provider, ToolRegistry(), self._sessions)

        await loop.process_direct("First question.", "test", "chat-1", "session-1")
        await loop.process_direct("Second question.", "test", "chat-1", "session-1")

        self.assertEqual(
            provider.complete_calls[1],
            (
                _SYSTEM_MESSAGE,
                HumanMessage(content="First question."),
                AIMessage(content="First answer."),
                HumanMessage(content="Second question."),
            ),
        )
        self.assertEqual(
            self._sessions.get_or_create("session-1").messages,
            (
                _SYSTEM_MESSAGE,
                HumanMessage(content="First question."),
                AIMessage(content="First answer."),
                HumanMessage(content="Second question."),
                AIMessage(content="Second answer."),
            ),
        )

    async def test_recreated_loop_recovers_history_from_the_workspace(self) -> None:
        first_loop = AgentLoop(
            AgentRunner(),
            ScriptedProvider((LLMResponse(content="First answer."),)),
            ToolRegistry(),
            self._sessions,
        )
        await first_loop.process_direct("First question.", "test", "chat-1", "session-1")

        provider = ScriptedProvider((LLMResponse(content="Second answer."),))
        recreated_loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            SessionManager(self._temporary_directory.name),
        )
        await recreated_loop.process_direct("Second question.", "test", "chat-1", "session-1")

        self.assertEqual(
            provider.complete_calls[0],
            (
                _SYSTEM_MESSAGE,
                HumanMessage(content="First question."),
                AIMessage(content="First answer."),
                HumanMessage(content="Second question."),
            ),
        )

    async def test_persists_tool_calls_and_results_in_execution_order(self) -> None:
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
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry((EchoTool(),)),
            self._sessions,
        )

        await loop.process_direct("Echo hello.", "test", "chat-1", "session-1")

        self.assertEqual(
            self._sessions.get_or_create("session-1").messages,
            (
                _SYSTEM_MESSAGE,
                HumanMessage(content="Echo hello."),
                AIMessage(content="I will echo it.", tool_calls=(request,)),
                ToolMessage(content="echo: hello", tool_call_id="call-1"),
                AIMessage(content="The value was echoed."),
            ),
        )

    async def test_empty_session_id_falls_back_to_channel_and_chat_id(self) -> None:
        provider = ScriptedProvider(
            (
                LLMResponse(content="First answer."),
                LLMResponse(content="Second answer."),
            )
        )
        loop = AgentLoop(AgentRunner(), provider, ToolRegistry(), self._sessions)

        await loop.process_direct("First question.", "alpha", "chat-1", "")
        await loop.process_direct("Second question.", "beta", "chat-1", "")

        self.assertEqual(
            provider.complete_calls[1],
            (_SYSTEM_MESSAGE, HumanMessage(content="Second question.")),
        )
        self.assertEqual(
            self._sessions.get_or_create("alpha:chat-1").messages,
            (
                _SYSTEM_MESSAGE,
                HumanMessage(content="First question."),
                AIMessage(content="First answer."),
            ),
        )
        self.assertEqual(
            self._sessions.get_or_create("beta:chat-1").messages,
            (
                _SYSTEM_MESSAGE,
                HumanMessage(content="Second question."),
                AIMessage(content="Second answer."),
            ),
        )

    async def test_runner_failure_keeps_existing_history_and_the_saved_user_message(self) -> None:
        previous_history = (HumanMessage(content="Previous question."),)
        self._sessions.save(
            self._sessions.get_or_create("session-1").with_messages(previous_history)
        )
        runner = FailingRunner()
        loop = AgentLoop(runner, ScriptedProvider(()), ToolRegistry(), self._sessions)

        with self.assertRaisesRegex(AgentRunnerError, "model unavailable"):
            await loop.process_direct("New question.", "test", "chat-1", "session-1")

        self.assertEqual(
            self._sessions.get_or_create("session-1").messages,
            (_SYSTEM_MESSAGE, *previous_history, HumanMessage(content="New question.")),
        )
        self.assertEqual(
            runner.received_spec.messages if runner.received_spec is not None else None,
            (_SYSTEM_MESSAGE, *previous_history, HumanMessage(content="New question.")),
        )
