"""Integration tests for AgentLoop session persistence."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import patch

from nanobot.agent import (
    AgentLoop,
    AgentRunner,
    AgentRunnerError,
    AgentRunResult,
    AgentRunSpec,
    ContextBuilder,
    estimate_messages_tokens,
)
from nanobot.bus import InboundMessage, MessageBus, OutboundMessage
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
from nanobot.tools import (
    Tool,
    ToolParameter,
    ToolRegistry,
    ToolResult,
    get_request_context,
)


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


class RequestContextTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="request_context",
            description="Record the current tool request context.",
        )
        self.received_context = None

    async def execute(self, **arguments: Any) -> ToolResult:
        del arguments
        self.received_context = get_request_context()
        return ToolResult(content="request context recorded")


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

        await _dispatch(loop, "First question.", "test", "chat-1", "session-1")
        await _dispatch(loop, "Second question.", "test", "chat-1", "session-1")

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

    async def test_persists_the_raw_user_message_when_a_skill_is_explicitly_active(self) -> None:
        skill_path = Path(self._temporary_directory.name) / "skills" / "github" / "SKILL.md"
        skill_path.parent.mkdir(parents=True)
        skill_path.write_text(
            "---\nname: github\n---\nGitHub turn instructions.\n",
            encoding="utf-8",
        )
        provider = ScriptedProvider((LLMResponse(content="Skill answer."),))
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            self._sessions,
            ContextBuilder(self._temporary_directory.name),
        )

        await _dispatch(loop, "Please use $github.", "test", "chat-1", "session-1")

        self.assertIn("GitHub turn instructions.", provider.complete_calls[0][0].content)
        self.assertEqual(
            self._sessions.get_or_create("session-1").messages,
            (
                HumanMessage(content="Please use $github."),
                AIMessage(content="Skill answer."),
            ),
        )

    @patch("nanobot.skills.loader.shutil.which", return_value=None)
    async def test_unavailable_skill_context_is_not_saved_in_session_messages(
        self,
        which: object,
    ) -> None:
        skill_path = Path(self._temporary_directory.name) / "skills" / "github" / "SKILL.md"
        skill_path.parent.mkdir(parents=True)
        skill_path.write_text(
            "---\nname: github\nnanobot:\n  requires:\n    bins: [\"gh\"]\n---\nGitHub turn instructions.\n",
            encoding="utf-8",
        )
        provider = ScriptedProvider((LLMResponse(content="Install gh first."),))
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            self._sessions,
            ContextBuilder(self._temporary_directory.name),
        )

        await _dispatch(loop, "Please use $github.", "test", "chat-1", "session-1")

        self.assertIn(
            "[Unavailable Skills requested for this turn]",
            provider.complete_calls[0][0].content,
        )
        self.assertNotIn("GitHub turn instructions.", provider.complete_calls[0][0].content)
        self.assertEqual(
            self._sessions.get_or_create("session-1").messages,
            (
                HumanMessage(content="Please use $github."),
                AIMessage(content="Install gh first."),
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
        await _dispatch(first_loop, "First question.", "test", "chat-1", "session-1")

        provider = ScriptedProvider((LLMResponse(content="Second answer."),))
        recreated_loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            SessionManager(self._temporary_directory.name),
            _context_builder(self._temporary_directory.name),
        )
        await _dispatch(recreated_loop, "Second question.", "test", "chat-1", "session-1")

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

        await _dispatch(loop, "Echo hello.", "test", "chat-1", "session-1")

        self.assertEqual(
            self._sessions.get_or_create("session-1").messages,
            (
                HumanMessage(content="Echo hello."),
                AIMessage(content="I will echo it.", tool_calls=(request,)),
                ToolMessage(content="echo: hello", tool_call_id="call-1"),
                AIMessage(content="The value was echoed."),
            ),
        )

    async def test_binds_request_context_only_while_the_runner_executes_tools(self) -> None:
        request = ToolCallRequest(
            id="call-1",
            name="request_context",
            arguments={},
        )
        provider = ScriptedProvider(
            (
                LLMResponse(tool_calls=(request,)),
                LLMResponse(content="Done."),
            )
        )
        tool = RequestContextTool()
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry((tool,)),
            self._sessions,
            _context_builder(self._temporary_directory.name),
        )

        await _dispatch(
            loop,
            "Record my request context.",
            "qq",
            "chat-1",
            "session-1",
            {"qq_chat_type": "c2c"},
        )

        self.assertIsNotNone(tool.received_context)
        self.assertEqual(tool.received_context.session_key, "session-1")
        self.assertEqual(tool.received_context.channel, "qq")
        self.assertEqual(tool.received_context.chat_id, "chat-1")
        self.assertEqual(tool.received_context.sender_id, "test-sender")
        self.assertEqual(tool.received_context.metadata, {"qq_chat_type": "c2c"})
        self.assertIsNone(get_request_context())

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

        await _dispatch(loop, "First question.", "alpha", "chat-1", "")
        await _dispatch(loop, "Second question.", "beta", "chat-1", "")

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

    async def test_runner_failure_keeps_existing_history_without_saving_current_user_message(self) -> None:
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
            await _dispatch(loop, "New question.", "test", "chat-1", "session-1")

        self.assertEqual(
            self._sessions.get_or_create("session-1").messages,
            previous_history,
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

        await _dispatch(loop, "First question.", "test", "chat-1", "session-1")
        soul_path.write_text("Second style.", encoding="utf-8")
        await _dispatch(loop, "Second question.", "test", "chat-1", "session-1")

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

        await _dispatch(loop, "New question.", "test", "chat-1", "session-1")

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

        await _dispatch(loop, "Current question.", "test", "chat-1", "session-1")

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

        await _dispatch(loop, "Current question.", "test", "chat-1", "session-1")

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

        await _dispatch(loop, "Current question.", "test", "chat-1", "session-1")
        await loop.wait_for_compactions()
        await _dispatch(loop, "Next question.", "test", "chat-1", "session-1")

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

        await _dispatch(loop, "Current question.", "test", "chat-1", "session-1")

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

        result = await _dispatch(loop, "Current question.", "test", "chat-1", "session-1")

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

        result = await _dispatch(loop, "Current question.", "test", "chat-1", "session-1")
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

        await _dispatch(loop, "First question.", "test", "chat-1", "session-1")
        await asyncio.wait_for(provider.summary_started.wait(), timeout=1)
        second_run = asyncio.create_task(
            _dispatch(loop, "Second question.", "test", "chat-1", "session-1")
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

        await _dispatch(loop, "Current question.", "test", "chat-1", "session-1")
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
            _dispatch(loop, "Current question.", "test", "chat-1", "session-1"),
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
            await _dispatch(loop, "Current question.", "test", "chat-1", "session-1")
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

        result = await _dispatch(loop, "Current question.", "test", "chat-1", "session-1")
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

        await _dispatch(loop, "First question.", "test", "chat-1", "session-1")
        await asyncio.wait_for(provider.memory_started.wait(), timeout=1)
        await _dispatch(loop, "Second question.", "test", "chat-2", "session-2")
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

        await _dispatch(loop, "Current question.", "test", "chat-1", "session-1")
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

    async def test_cron_message_uses_session_context_and_persists(self) -> None:
        provider = ScriptedProvider(
            (
                LLMResponse(content="Initial answer."),
                LLMResponse(content="Scheduled answer."),
            )
        )
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            self._sessions,
            _context_builder(self._temporary_directory.name),
        )

        await _dispatch(loop, "Initial question.", "test", "chat-1", "session-1")
        saved_messages = self._sessions.get_or_create("session-1").messages
        await _dispatch(
            loop,
            "Run scheduled work.",
            "test",
            "chat-1",
            "session-1",
            {"source": "cron"},
        )

        updated_messages = self._sessions.get_or_create("session-1").messages
        self.assertEqual(provider.complete_calls[1][1:-1], saved_messages)
        self.assertEqual(provider.complete_calls[1][-1], HumanMessage(content="Run scheduled work."))
        self.assertEqual(updated_messages[: len(saved_messages)], saved_messages)
        self.assertEqual(updated_messages[-2], HumanMessage(content="Run scheduled work."))
        self.assertEqual(updated_messages[-1], AIMessage(content="Scheduled answer."))

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

        result = await _dispatch(loop, "Current question.", "test", "chat-1", "session-1")

        self.assertIn("请新开会话", result.content)
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


async def _dispatch(
    loop: AgentLoop,
    content: str,
    channel: str,
    chat_id: str,
    session_id: str,
    metadata: dict[str, Any] | None = None,
) -> OutboundMessage:
    """Exercise AgentLoop dispatch without restoring a public direct-call API."""

    inbound = InboundMessage(
        channel=channel,
        chat_id=chat_id,
        sender_id="test-sender",
        session_id=session_id,
        content=content,
        metadata=metadata or {},
    )
    invocation = loop._command_router.parse(content)
    if loop._command_router.is_stop_command(invocation):
        if invocation is None:
            raise AssertionError("/stop must parse as a command")
        return await loop._run_stop_command(inbound, invocation)
    return await loop._dispatch_non_stop_message(inbound, invocation)


def _is_summary_request(messages: tuple[BaseMessage, ...]) -> bool:
    return bool(messages) and isinstance(messages[0], SystemMessage) and messages[0].content.startswith(
        "Summarize this conversation"
    )


def _is_memory_request(messages: tuple[BaseMessage, ...]) -> bool:
    return bool(messages) and isinstance(messages[0], SystemMessage) and messages[0].content.startswith(
        "Maintain the long-term memory"
    )
