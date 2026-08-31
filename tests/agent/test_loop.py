"""Integration tests for AgentLoop session persistence."""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from nanobot.agent import (
    AgentLoop,
    AgentRunner,
    AgentRunnerError,
    AgentRunResult,
    AgentRunSpec,
    ContextBuilder,
    estimate_messages_tokens,
)
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

    async def test_continuous_messages_use_the_session_id_without_persisting_the_system_prompt(self) -> None:
        provider = ScriptedProvider(
            (
                LLMResponse(content="First answer."),
                LLMResponse(content="Second answer."),
            )
        )
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            self._sessions,
            _context_builder(self._temporary_directory.name),
        )

        await loop.process_direct("First question.", "test", "chat-1", "session-1")
        await loop.process_direct("Second question.", "test", "chat-1", "session-1")

        self.assertEqual(
            provider.complete_calls[1],
            (
                _system_message(self._temporary_directory.name),
                HumanMessage(content="First question."),
                AIMessage(content="First answer."),
                HumanMessage(content="Second question."),
            ),
        )
        self.assertEqual(
            self._sessions.get_or_create("session-1").messages,
            (
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
            _context_builder(self._temporary_directory.name),
        )
        await first_loop.process_direct("First question.", "test", "chat-1", "session-1")

        provider = ScriptedProvider((LLMResponse(content="Second answer."),))
        recreated_loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            SessionManager(self._temporary_directory.name),
            _context_builder(self._temporary_directory.name),
        )
        await recreated_loop.process_direct("Second question.", "test", "chat-1", "session-1")

        self.assertEqual(
            provider.complete_calls[0],
            (
                _system_message(self._temporary_directory.name),
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
            _context_builder(self._temporary_directory.name),
        )

        await loop.process_direct("Echo hello.", "test", "chat-1", "session-1")

        self.assertEqual(
            self._sessions.get_or_create("session-1").messages,
            (
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
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            self._sessions,
            _context_builder(self._temporary_directory.name),
        )

        await loop.process_direct("First question.", "alpha", "chat-1", "")
        await loop.process_direct("Second question.", "beta", "chat-1", "")

        self.assertEqual(
            provider.complete_calls[1],
            (_system_message(self._temporary_directory.name), HumanMessage(content="Second question.")),
        )
        self.assertEqual(
            self._sessions.get_or_create("alpha:chat-1").messages,
            (
                HumanMessage(content="First question."),
                AIMessage(content="First answer."),
            ),
        )
        self.assertEqual(
            self._sessions.get_or_create("beta:chat-1").messages,
            (
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
        loop = AgentLoop(
            runner,
            ScriptedProvider(()),
            ToolRegistry(),
            self._sessions,
            _context_builder(self._temporary_directory.name),
        )

        with self.assertRaisesRegex(AgentRunnerError, "model unavailable"):
            await loop.process_direct("New question.", "test", "chat-1", "session-1")

        self.assertEqual(
            self._sessions.get_or_create("session-1").messages,
            (*previous_history, HumanMessage(content="New question.")),
        )
        self.assertEqual(
            runner.received_spec.messages if runner.received_spec is not None else None,
            (
                _system_message(self._temporary_directory.name),
                *previous_history,
                HumanMessage(content="New question."),
            ),
        )

    async def test_rebuilds_the_system_prompt_for_each_request(self) -> None:
        soul_path = Path(self._temporary_directory.name) / "SOUL.md"
        soul_path.write_text("First style.", encoding="utf-8")
        provider = ScriptedProvider(
            (
                LLMResponse(content="First answer."),
                LLMResponse(content="Second answer."),
            )
        )
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            self._sessions,
            _context_builder(self._temporary_directory.name),
        )

        await loop.process_direct("First question.", "test", "chat-1", "session-1")
        soul_path.write_text("Second style.", encoding="utf-8")
        await loop.process_direct("Second question.", "test", "chat-1", "session-1")

        first_system_message = provider.complete_calls[0][0]
        second_system_message = provider.complete_calls[1][0]
        self.assertIsInstance(first_system_message, SystemMessage)
        self.assertIsInstance(second_system_message, SystemMessage)
        self.assertIn("First style.", first_system_message.content)
        self.assertIn("Second style.", second_system_message.content)
        self.assertNotIn("First style.", second_system_message.content)
        self.assertFalse(
            any(
                isinstance(message, SystemMessage)
                for message in self._sessions.get_or_create("session-1").messages
            )
        )

    async def test_replaces_a_legacy_persisted_system_message(self) -> None:
        previous_history = (
            SystemMessage(content="Legacy system prompt."),
            HumanMessage(content="Previous question."),
        )
        self._sessions.save(
            self._sessions.get_or_create("session-1").with_messages(previous_history)
        )
        provider = ScriptedProvider((LLMResponse(content="New answer."),))
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            self._sessions,
            _context_builder(self._temporary_directory.name),
        )

        await loop.process_direct("New question.", "test", "chat-1", "session-1")

        self.assertEqual(
            provider.complete_calls[0],
            (
                _system_message(self._temporary_directory.name),
                HumanMessage(content="Previous question."),
                HumanMessage(content="New question."),
            ),
        )
        self.assertEqual(
            self._sessions.get_or_create("session-1").messages,
            (
                HumanMessage(content="Previous question."),
                HumanMessage(content="New question."),
                AIMessage(content="New answer."),
            ),
        )

    async def test_trims_only_the_llm_context_and_preserves_complete_session_history(self) -> None:
        older_turn = (
            HumanMessage(content="Older question " * 30),
            AIMessage(content="Older answer."),
        )
        recent_turn = (
            HumanMessage(content="Recent question."),
            AIMessage(content="Recent answer."),
        )
        self._sessions.save(
            self._sessions.get_or_create("session-1").with_messages(
                (*older_turn, *recent_turn)
            )
        )
        provider = ScriptedProvider((LLMResponse(content="Current answer."),))
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            self._sessions,
            _context_builder(
                self._temporary_directory.name,
                estimate_messages_tokens(recent_turn),
            ),
        )

        await loop.process_direct("Current question.", "test", "chat-1", "session-1")

        self.assertEqual(
            provider.complete_calls[0],
            (
                _system_message(self._temporary_directory.name),
                *recent_turn,
                HumanMessage(content="Current question."),
            ),
        )
        self.assertEqual(
            self._sessions.get_or_create("session-1").messages,
            (
                *older_turn,
                *recent_turn,
                HumanMessage(content="Current question."),
                AIMessage(content="Current answer."),
            ),
        )

    async def test_recreated_session_manager_still_trims_restored_history(self) -> None:
        older_turn = (
            HumanMessage(content="Older question " * 30),
            AIMessage(content="Older answer."),
        )
        recent_turn = (
            HumanMessage(content="Recent question."),
            AIMessage(content="Recent answer."),
        )
        self._sessions.save(
            self._sessions.get_or_create("session-1").with_messages(
                (*older_turn, *recent_turn)
            )
        )
        provider = ScriptedProvider((LLMResponse(content="Current answer."),))
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            SessionManager(self._temporary_directory.name),
            _context_builder(
                self._temporary_directory.name,
                estimate_messages_tokens(recent_turn),
            ),
        )

        await loop.process_direct("Current question.", "test", "chat-1", "session-1")

        self.assertEqual(provider.complete_calls[0][1:-1], recent_turn)
        self.assertEqual(
            provider.complete_calls[0].count(HumanMessage(content="Current question.")),
            1,
        )


def _system_message(workspace: str) -> SystemMessage:
    return SystemMessage(content=ContextBuilder(workspace).build_system_prompt())


def _context_builder(workspace: str, history_token_budget: int = 64_000) -> ContextBuilder:
    return ContextBuilder(workspace, history_token_budget)
