"""Integration tests for AgentLoop session persistence."""

from __future__ import annotations

import asyncio
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
from nanobot.bus import MessageBus
from nanobot.memory import MemoryConsolidator, MemoryStore
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
from nanobot.session import SessionCompactor, SessionManager
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


class BackgroundCompactionProvider(LLMProvider):
    def __init__(
        self,
        responses: Sequence[LLMResponse],
        summary_response: LLMResponse | Exception,
        *,
        block_summary: bool = False,
    ) -> None:
        self._responses = iter(responses)
        self._summary_response = summary_response
        self.complete_calls: list[tuple[BaseMessage, ...]] = []
        self.summary_started = asyncio.Event()
        self.summary_finished = asyncio.Event()
        self._summary_release = asyncio.Event()
        if not block_summary:
            self._summary_release.set()

    async def complete(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        del tools, max_tokens, temperature
        request = tuple(messages)
        self.complete_calls.append(request)
        if _is_summary_request(request):
            self.summary_started.set()
            try:
                await self._summary_release.wait()
                if isinstance(self._summary_response, Exception):
                    raise self._summary_response
                return self._summary_response
            finally:
                self.summary_finished.set()
        return next(self._responses)

    def release_summary(self) -> None:
        self._summary_release.set()

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        raise AssertionError("AgentLoop must not use streaming")


class BackgroundMemoryProvider(LLMProvider):
    def __init__(
        self,
        responses: Sequence[LLMResponse],
        memory_response: LLMResponse | Exception,
        *,
        block_memory: bool = False,
    ) -> None:
        self._responses = iter(responses)
        self._memory_response = memory_response
        self.normal_calls: list[tuple[BaseMessage, ...]] = []
        self.memory_requests: list[tuple[BaseMessage, ...]] = []
        self.memory_started = asyncio.Event()
        self.memory_finished = asyncio.Event()
        self._memory_release = asyncio.Event()
        self._active_memory_calls = 0
        self.maximum_concurrent_memory_calls = 0
        if not block_memory:
            self._memory_release.set()

    async def complete(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        del tools, max_tokens, temperature
        request = tuple(messages)
        if _is_memory_request(request):
            self.memory_requests.append(request)
            self.memory_started.set()
            self._active_memory_calls += 1
            self.maximum_concurrent_memory_calls = max(
                self.maximum_concurrent_memory_calls,
                self._active_memory_calls,
            )
            try:
                await self._memory_release.wait()
                if isinstance(self._memory_response, Exception):
                    raise self._memory_response
                return self._memory_response
            finally:
                self._active_memory_calls -= 1
                self.memory_finished.set()
        self.normal_calls.append(request)
        return next(self._responses)

    def release_memory(self) -> None:
        self._memory_release.set()

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

    async def test_compaction_preserves_full_history_and_uses_summary_on_the_next_turn(self) -> None:
        request = ToolCallRequest(
            id="call-1",
            name="echo",
            arguments={"value": "old"},
        )
        old_turn = (
            HumanMessage(content="Old question " * 40),
            AIMessage(content="Calling echo.", tool_calls=(request,)),
            ToolMessage(content="echo: old", tool_call_id="call-1"),
            AIMessage(content="Old answer."),
        )
        recent_turn = (
            HumanMessage(content="Recent question."),
            AIMessage(content="Recent answer."),
        )
        current_turn = (
            HumanMessage(content="Current question."),
            AIMessage(content="Current answer."),
        )
        self._sessions.save(
            self._sessions.get_or_create("session-1").with_messages(
                (*old_turn, *recent_turn)
            )
        )
        provider = ScriptedProvider(
            (
                LLMResponse(content="Current answer."),
                LLMResponse(content="Earlier turns were summarized."),
                LLMResponse(content="Next answer."),
            )
        )
        compactor = SessionCompactor(
            provider,
            token_threshold=estimate_messages_tokens((*old_turn, *recent_turn)),
            recent_token_budget=estimate_messages_tokens(current_turn),
        )
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry((EchoTool(),)),
            self._sessions,
            _context_builder(self._temporary_directory.name),
            session_compactor=compactor,
        )

        await loop.process_direct("Current question.", "test", "chat-1", "session-1")
        await loop.wait_for_compactions()
        await loop.process_direct("Next question.", "test", "chat-1", "session-1")

        session = self._sessions.get_or_create("session-1")
        self.assertEqual(
            session.messages,
            (
                *old_turn,
                *recent_turn,
                *current_turn,
                HumanMessage(content="Next question."),
                AIMessage(content="Next answer."),
            ),
        )
        self.assertEqual(session.summary, "Earlier turns were summarized.")
        self.assertEqual(session.summary_until, len((*old_turn, *recent_turn)))
        self.assertEqual(
            provider.complete_calls[2],
            (
                SystemMessage(
                    content=(
                        f"{_system_message(self._temporary_directory.name).content}"
                        "\n\n## Conversation Summary\n\nEarlier turns were summarized."
                    )
                ),
                *current_turn,
                HumanMessage(content="Next question."),
            ),
        )

    async def test_request_uses_history_clipping_before_background_compaction(self) -> None:
        old_turn = (
            HumanMessage(content="Old question " * 40),
            AIMessage(content="Old answer."),
        )
        recent_turn = (
            HumanMessage(content="Recent question."),
            AIMessage(content="Recent answer."),
        )
        current_turn = (
            HumanMessage(content="Current question."),
            AIMessage(content="Current answer."),
        )
        self._sessions.save(
            self._sessions.get_or_create("session-1").with_messages(
                (*old_turn, *recent_turn)
            )
        )
        provider = ScriptedProvider(
            (
                LLMResponse(content="Current answer."),
                LLMResponse(content="The old turn was summarized."),
            )
        )
        compactor = SessionCompactor(
            provider,
            token_threshold=estimate_messages_tokens((*recent_turn, *current_turn)),
            recent_token_budget=estimate_messages_tokens(current_turn),
        )
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            self._sessions,
            _context_builder(
                self._temporary_directory.name,
                estimate_messages_tokens(recent_turn),
            ),
            session_compactor=compactor,
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
        self.assertEqual(len(provider.complete_calls), 1)
        await loop.wait_for_compactions()
        session = self._sessions.get_or_create("session-1")
        self.assertEqual(session.messages, (*old_turn, *recent_turn, *current_turn))
        self.assertEqual(session.summary, "The old turn was summarized.")
        self.assertEqual(session.summary_until, len((*old_turn, *recent_turn)))

    async def test_saves_completed_messages_before_starting_background_compaction(self) -> None:
        old_turn = (
            HumanMessage(content="Old question " * 40),
            AIMessage(content="Old answer."),
        )
        current_turn = (
            HumanMessage(content="Current question."),
            AIMessage(content="Current answer."),
        )
        self._sessions.save(
            self._sessions.get_or_create("session-1").with_messages(old_turn)
        )
        provider = BackgroundCompactionProvider(
            (LLMResponse(content="Current answer."),),
            LLMResponse(content="The old turn was summarized."),
            block_summary=True,
        )
        compactor = SessionCompactor(
            provider,
            token_threshold=estimate_messages_tokens(current_turn) + 1,
            recent_token_budget=estimate_messages_tokens(current_turn),
        )
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            self._sessions,
            _context_builder(self._temporary_directory.name),
            session_compactor=compactor,
        )

        result = await loop.process_direct("Current question.", "test", "chat-1", "session-1")

        self.assertEqual(result.content, "Current answer.")
        self.assertEqual(len(provider.complete_calls), 1)
        self.assertEqual(
            self._sessions.get_or_create("session-1").messages,
            (*old_turn, *current_turn),
        )
        await asyncio.wait_for(provider.summary_started.wait(), timeout=1)
        self.assertIsNone(self._sessions.get_or_create("session-1").summary)

        provider.release_summary()
        await asyncio.wait_for(provider.summary_finished.wait(), timeout=1)
        await loop.wait_for_compactions()

        session = self._sessions.get_or_create("session-1")
        self.assertEqual(session.summary, "The old turn was summarized.")
        self.assertEqual(session.summary_until, len(old_turn))

    async def test_background_compaction_failure_keeps_saved_session_data(self) -> None:
        old_turn = (
            HumanMessage(content="Old question " * 40),
            AIMessage(content="Old answer."),
        )
        current_turn = (
            HumanMessage(content="Current question."),
            AIMessage(content="Current answer."),
        )
        self._sessions.save(
            self._sessions.get_or_create("session-1").with_messages(old_turn)
        )
        provider = BackgroundCompactionProvider(
            (LLMResponse(content="Current answer."),),
            RuntimeError("summary unavailable"),
        )
        compactor = SessionCompactor(
            provider,
            token_threshold=estimate_messages_tokens(current_turn) + 1,
            recent_token_budget=estimate_messages_tokens(current_turn),
        )
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            self._sessions,
            _context_builder(self._temporary_directory.name),
            session_compactor=compactor,
        )

        result = await loop.process_direct("Current question.", "test", "chat-1", "session-1")
        await loop.wait_for_compactions()

        session = self._sessions.get_or_create("session-1")
        self.assertEqual(result.content, "Current answer.")
        self.assertEqual(session.messages, (*old_turn, *current_turn))
        self.assertIsNone(session.summary)
        self.assertEqual(session.summary_until, 0)

    async def test_session_lock_prevents_background_compaction_from_overwriting_newer_messages(self) -> None:
        old_turn = (
            HumanMessage(content="Old question " * 40),
            AIMessage(content="Old answer."),
        )
        first_turn = (
            HumanMessage(content="First question."),
            AIMessage(content="First answer."),
        )
        second_turn = (
            HumanMessage(content="Second question."),
            AIMessage(content="Second answer."),
        )
        self._sessions.save(
            self._sessions.get_or_create("session-1").with_messages(old_turn)
        )
        provider = BackgroundCompactionProvider(
            (
                LLMResponse(content="First answer."),
                LLMResponse(content="Second answer."),
            ),
            LLMResponse(content="The old turn was summarized."),
            block_summary=True,
        )
        compactor = SessionCompactor(
            provider,
            token_threshold=estimate_messages_tokens((*first_turn, *second_turn)) + 1,
            recent_token_budget=estimate_messages_tokens(first_turn),
        )
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            self._sessions,
            _context_builder(self._temporary_directory.name),
            session_compactor=compactor,
        )

        await loop.process_direct("First question.", "test", "chat-1", "session-1")
        await asyncio.wait_for(provider.summary_started.wait(), timeout=1)
        second_run = asyncio.create_task(
            loop.process_direct("Second question.", "test", "chat-1", "session-1")
        )
        await asyncio.sleep(0)
        self.assertFalse(second_run.done())

        provider.release_summary()
        await second_run
        await loop.wait_for_compactions()

        session = self._sessions.get_or_create("session-1")
        self.assertEqual(session.messages, (*old_turn, *first_turn, *second_turn))
        self.assertEqual(session.summary, "The old turn was summarized.")
        self.assertEqual(session.summary_until, len(old_turn))

    async def test_close_cancels_pending_background_compaction(self) -> None:
        old_turn = (
            HumanMessage(content="Old question " * 40),
            AIMessage(content="Old answer."),
        )
        current_turn = (
            HumanMessage(content="Current question."),
            AIMessage(content="Current answer."),
        )
        self._sessions.save(
            self._sessions.get_or_create("session-1").with_messages(old_turn)
        )
        provider = BackgroundCompactionProvider(
            (LLMResponse(content="Current answer."),),
            LLMResponse(content="unused"),
            block_summary=True,
        )
        compactor = SessionCompactor(
            provider,
            token_threshold=estimate_messages_tokens(current_turn) + 1,
            recent_token_budget=estimate_messages_tokens(current_turn),
        )
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            self._sessions,
            _context_builder(self._temporary_directory.name),
            session_compactor=compactor,
        )

        await loop.process_direct("Current question.", "test", "chat-1", "session-1")
        await asyncio.wait_for(provider.summary_started.wait(), timeout=1)
        await loop.close()

        self.assertTrue(provider.summary_finished.is_set())
        self.assertIsNone(self._sessions.get_or_create("session-1").summary)

    async def test_schedules_memory_after_saving_a_complete_snapshot_without_blocking(self) -> None:
        previous_messages = (
            HumanMessage(content="Earlier question."),
            AIMessage(content="Earlier answer."),
        )
        self._sessions.save(
            self._sessions.get_or_create("session-1")
            .with_messages(previous_messages)
            .with_summary("Internal session summary.", len(previous_messages))
        )
        provider = BackgroundMemoryProvider(
            (LLMResponse(content="Current answer."),),
            LLMResponse(content="The user prefers concise answers."),
            block_memory=True,
        )
        store = MemoryStore(self._temporary_directory.name)
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            self._sessions,
            _context_builder(self._temporary_directory.name),
            memory_store=store,
            memory_consolidator=MemoryConsolidator(provider, store),
        )

        result = await asyncio.wait_for(
            loop.process_direct("Current question.", "test", "chat-1", "session-1"),
            timeout=1,
        )

        self.assertEqual(result.content, "Current answer.")
        self.assertEqual(
            self._sessions.get_or_create("session-1").messages,
            (
                *previous_messages,
                HumanMessage(content="Current question."),
                AIMessage(content="Current answer."),
            ),
        )
        events = store.read_events_after(0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].session_key, "session-1")
        self.assertEqual(
            events[0].messages,
            (
                HumanMessage(content="Current question."),
                AIMessage(content="Current answer."),
            ),
        )
        self.assertFalse(any(isinstance(message, SystemMessage) for message in events[0].messages))
        self.assertNotIn(
            "Internal session summary.",
            tuple(message.content for message in events[0].messages),
        )
        await asyncio.wait_for(provider.memory_started.wait(), timeout=1)
        self.assertEqual(store.read(), "")
        request = provider.memory_requests[0]
        self.assertIn('"content":"Current question."', request[1].content)
        self.assertIn('"content":"Current answer."', request[1].content)

        provider.release_memory()
        await loop.wait_for_memory_consolidations()

        self.assertEqual(store.read(), "The user prefers concise answers.")
        self.assertEqual(store.read_cursor(), 1)
        self.assertEqual(len(provider.normal_calls), 1)
        self.assertEqual(len(provider.memory_requests), 1)

    async def test_runner_failure_does_not_schedule_memory_consolidation(self) -> None:
        provider = BackgroundMemoryProvider((), LLMResponse(content="unused"))
        store = MemoryStore(self._temporary_directory.name)
        loop = AgentLoop(
            FailingRunner(),
            provider,
            ToolRegistry(),
            self._sessions,
            _context_builder(self._temporary_directory.name),
            memory_store=store,
            memory_consolidator=MemoryConsolidator(
                provider,
                store,
            ),
        )

        with self.assertRaises(AgentRunnerError):
            await loop.process_direct("Current question.", "test", "chat-1", "session-1")
        await asyncio.sleep(0)

        self.assertEqual(provider.memory_requests, [])
        self.assertEqual(store.read_events_after(0), ())

    async def test_memory_failure_does_not_affect_the_saved_user_result(self) -> None:
        provider = BackgroundMemoryProvider(
            (LLMResponse(content="Current answer."),),
            RuntimeError("memory provider unavailable"),
        )
        store = MemoryStore(self._temporary_directory.name)
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            self._sessions,
            _context_builder(self._temporary_directory.name),
            memory_store=store,
            memory_consolidator=MemoryConsolidator(provider, store),
        )

        result = await loop.process_direct("Current question.", "test", "chat-1", "session-1")
        await loop.wait_for_memory_consolidations()

        self.assertEqual(result.content, "Current answer.")
        self.assertEqual(store.read(), "")
        self.assertEqual(store.read_cursor(), 0)
        self.assertEqual(len(store.read_events_after(0)), 1)
        self.assertEqual(
            self._sessions.get_or_create("session-1").messages,
            (
                HumanMessage(content="Current question."),
                AIMessage(content="Current answer."),
            ),
        )

    async def test_memory_consolidation_is_serialized_for_all_sessions_in_one_workspace(self) -> None:
        provider = BackgroundMemoryProvider(
            (
                LLMResponse(content="First answer."),
                LLMResponse(content="Second answer."),
            ),
            LLMResponse(content="Stable project convention."),
            block_memory=True,
        )
        store = MemoryStore(self._temporary_directory.name)
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            self._sessions,
            _context_builder(self._temporary_directory.name),
            memory_store=store,
            memory_consolidator=MemoryConsolidator(
                provider,
                store,
            ),
        )

        await loop.process_direct("First question.", "test", "chat-1", "session-1")
        await asyncio.wait_for(provider.memory_started.wait(), timeout=1)
        await loop.process_direct("Second question.", "test", "chat-2", "session-2")
        await asyncio.sleep(0)

        self.assertEqual(provider.maximum_concurrent_memory_calls, 1)
        provider.release_memory()
        await loop.wait_for_memory_consolidations()

        self.assertEqual(provider.maximum_concurrent_memory_calls, 1)
        self.assertEqual(len(provider.memory_requests), 2)
        self.assertEqual(
            tuple(event.session_key for event in store.read_events_after(0)),
            ("session-1", "session-2"),
        )
        self.assertEqual(store.read_cursor(), 2)
        self.assertIn('"content":"First question."', provider.memory_requests[0][1].content)
        self.assertIn('"content":"Second question."', provider.memory_requests[1][1].content)

    async def test_close_cancels_pending_memory_consolidation(self) -> None:
        provider = BackgroundMemoryProvider(
            (LLMResponse(content="Current answer."),),
            LLMResponse(content="unused"),
            block_memory=True,
        )
        store = MemoryStore(self._temporary_directory.name)
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            self._sessions,
            _context_builder(self._temporary_directory.name),
            memory_store=store,
            memory_consolidator=MemoryConsolidator(provider, store),
        )

        await loop.process_direct("Current question.", "test", "chat-1", "session-1")
        await asyncio.wait_for(provider.memory_started.wait(), timeout=1)
        await loop.close()

        self.assertTrue(provider.memory_finished.is_set())
        self.assertEqual(store.read(), "")
        self.assertEqual(store.read_cursor(), 0)
        self.assertEqual(len(store.read_events_after(0)), 1)

    async def test_recreated_loop_resumes_unprocessed_memory_events_on_startup(self) -> None:
        store = MemoryStore(self._temporary_directory.name)
        event = store.append_event(
            "session-1",
            (
                HumanMessage(content="Remember this decision."),
                AIMessage(content="I will preserve it."),
            ),
        )
        provider = BackgroundMemoryProvider(
            (),
            LLMResponse(content="A durable project decision."),
        )
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            self._sessions,
            _context_builder(self._temporary_directory.name),
            message_bus=MessageBus(),
            memory_store=store,
            memory_consolidator=MemoryConsolidator(provider, store),
        )
        run_task = asyncio.create_task(loop.run())

        await asyncio.wait_for(provider.memory_started.wait(), timeout=1)
        await loop.wait_for_memory_consolidations()

        self.assertEqual(store.read_cursor(), event.event_id)
        self.assertEqual(store.read(), "A durable project decision.")

        run_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await run_task

    async def test_skips_memory_consolidation_for_non_normal_user_messages(self) -> None:
        provider = BackgroundMemoryProvider(
            (
                LLMResponse(content="Ephemeral answer."),
                LLMResponse(content="System answer."),
                LLMResponse(content="Command answer."),
            ),
            LLMResponse(content="unused"),
        )
        store = MemoryStore(self._temporary_directory.name)
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            self._sessions,
            _context_builder(self._temporary_directory.name),
            memory_store=store,
            memory_consolidator=MemoryConsolidator(
                provider,
                store,
            ),
        )

        await loop.process_direct(
            "Ephemeral question.",
            "test",
            "chat-1",
            "session-1",
            {"ephemeral": True},
        )
        await loop.process_direct(
            "System question.",
            "test",
            "chat-2",
            "session-2",
            {"message_type": "system"},
        )
        await loop.process_direct("/help", "test", "chat-3", "session-3")
        await asyncio.sleep(0)

        self.assertEqual(provider.memory_requests, [])
        self.assertEqual(store.read_events_after(0), ())

    async def test_returns_a_user_facing_message_when_required_context_exceeds_the_window(self) -> None:
        provider = ScriptedProvider(())
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            self._sessions,
            ContextBuilder(
                self._temporary_directory.name,
                context_window_tokens=1,
                output_token_reserve=0,
            ),
        )

        result = await loop.process_direct("Current question.", "test", "chat-1", "session-1")

        self.assertEqual(result.stop_reason, "context_window_exceeded")
        self.assertIn("请新开会话", result.content or "")
        self.assertEqual(provider.complete_calls, [])
        self.assertEqual(self._sessions.get_or_create("session-1").messages, ())


def _system_message(workspace: str) -> SystemMessage:
    return SystemMessage(content=ContextBuilder(workspace).build_system_prompt())


def _context_builder(workspace: str, history_budget: int = 64_000) -> ContextBuilder:
    system_message = SystemMessage(content=ContextBuilder(workspace).build_system_prompt())
    return ContextBuilder(
        workspace,
        context_window_tokens=estimate_messages_tokens((system_message,)) + history_budget + 128,
        output_token_reserve=0,
    )


def _is_summary_request(messages: tuple[BaseMessage, ...]) -> bool:
    return bool(messages) and isinstance(messages[0], SystemMessage) and messages[0].content.startswith(
        "Summarize this conversation"
    )


def _is_memory_request(messages: tuple[BaseMessage, ...]) -> bool:
    return bool(messages) and isinstance(messages[0], SystemMessage) and messages[0].content.startswith(
        "Update the long-term memory"
    )
