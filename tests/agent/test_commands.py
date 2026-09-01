"""Focused tests for Agent command routing and session controls."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Awaitable, Callable, Sequence

from nanobot.agent import AgentLoop, AgentRunner, CommandRouter, ContextBuilder
from nanobot.bus import InboundMessage, MessageBus, OutboundMessage
from nanobot.memory import MemoryConsolidator, MemoryStore
from nanobot.providers import AIMessage, BaseMessage, HumanMessage, LLMProvider, LLMResponse
from nanobot.session import Session, SessionCompactor, SessionManager
from nanobot.tools import Tool, ToolRegistry


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
        raise AssertionError("Command tests must not use streaming")


class BlockingProvider(LLMProvider):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.release = asyncio.Event()
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
        self.started.set()
        try:
            await self.release.wait()
            return LLMResponse(content="Completed.")
        finally:
            self.finished.set()

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        raise AssertionError("Command tests must not use streaming")


class RecordingCompactor(SessionCompactor):
    def __init__(self, provider: LLMProvider) -> None:
        super().__init__(provider, token_threshold=2, recent_token_budget=0)
        self.compact_calls: list[Session] = []

    async def compact(self, session: Session) -> Session:
        raise AssertionError("/compact must use manual compaction")

    async def compact_manually(self, session: Session) -> Session:
        self.compact_calls.append(session)
        if not session.messages:
            return session
        return session.with_summary("Compacted session.", len(session.messages))


class CommandRouterTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._sessions = SessionManager(self._temporary_directory.name)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_parses_case_insensitive_commands_and_dispatches_help(self) -> None:
        router = CommandRouter(self._sessions)
        invocation = router.parse("  /HeLp  ")

        self.assertEqual(invocation.name, "help")
        self.assertEqual(invocation.arguments, ())
        self.assertIsNone(router.parse("ordinary text"))
        reply = await router.route(
            InboundMessage("test", "chat-1", "sender-1", "session-1", " /HELP "),
            "session-1",
            self._sessions.get_or_create("session-1"),
            invocation,
        )

        self.assertEqual(reply.channel, "test")
        self.assertEqual(reply.chat_id, "chat-1")
        self.assertIn("/new", reply.content)
        self.assertIn("/memory", reply.content)

    async def test_rejects_duplicate_registered_command_names(self) -> None:
        router = CommandRouter(self._sessions)

        async def ignored(_context: object) -> str:
            return "unused"

        with self.assertRaises(ValueError):
            router.register("NEW", "Duplicate", ignored)

    async def test_handler_failure_becomes_a_user_facing_reply(self) -> None:
        router = CommandRouter(self._sessions)

        async def failing(_context: object) -> str:
            raise RuntimeError("unexpected command failure")

        router.register("broken", "Used to verify failure handling.", failing)
        reply = await router.route(
            InboundMessage("test", "chat-1", "sender-1", "session-1", "/broken"),
            "session-1",
            self._sessions.get_or_create("session-1"),
        )

        self.assertEqual(reply.content, "命令执行失败，请稍后重试。")


class AgentLoopCommandTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._sessions = SessionManager(self._temporary_directory.name)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_plain_text_continues_to_the_provider(self) -> None:
        provider = ScriptedProvider((LLMResponse(content="Normal reply."),))
        loop = self._loop(provider)

        result = await _dispatch(loop, "Hello", "test", "chat-1", "session-1")

        self.assertEqual(result.content, "Normal reply.")
        self.assertEqual(len(provider.complete_calls), 1)

    async def test_new_clears_and_persists_the_current_session_without_a_provider_call(self) -> None:
        previous = self._sessions.get_or_create("session-1").with_messages(
            (HumanMessage(content="Earlier question."), AIMessage(content="Earlier answer."))
        ).with_summary("Earlier summary.", 2)
        self._sessions.save(previous)
        provider = ScriptedProvider(())
        loop = self._loop(provider)

        result = await _dispatch(loop, "  /NEW  ", "test", "chat-1", "session-1")
        recovered = SessionManager(self._temporary_directory.name).get_or_create("session-1")

        self.assertIn("已开始新的会话", result.content)
        self.assertEqual(recovered.messages, ())
        self.assertIsNone(recovered.summary)
        self.assertEqual(recovered.summary_until, 0)
        self.assertEqual(provider.complete_calls, [])

    async def test_help_unknown_and_invalid_argument_commands_do_not_call_the_provider(self) -> None:
        provider = ScriptedProvider(())
        loop = self._loop(provider)

        help_result = await _dispatch(loop, "/help", "test", "chat-1", "session-1")
        unknown_result = await _dispatch(loop, "/unknown", "test", "chat-1", "session-1")
        invalid_result = await _dispatch(loop, "/new extra", "test", "chat-1", "session-1")

        self.assertIn("可用命令", help_result.content)
        self.assertIn("未知命令", unknown_result.content)
        self.assertIn("不接受参数", invalid_result.content)
        self.assertIsInstance(help_result, OutboundMessage)
        self.assertIsInstance(unknown_result, OutboundMessage)
        self.assertIsInstance(invalid_result, OutboundMessage)
        self.assertEqual(provider.complete_calls, [])
        self.assertEqual(self._sessions.get_or_create("session-1").messages, ())

    async def test_compact_uses_the_existing_session_compactor(self) -> None:
        provider = ScriptedProvider(())
        compactor = RecordingCompactor(provider)
        session = self._sessions.get_or_create("session-1").with_messages(
            (HumanMessage(content="Question."), AIMessage(content="Answer."))
        )
        self._sessions.save(session)
        loop = self._loop(provider, session_compactor=compactor)

        result = await _dispatch(loop, "/compact", "test", "chat-1", "session-1")

        self.assertIn("已整理", result.content)
        self.assertEqual(len(compactor.compact_calls), 1)
        recovered = self._sessions.get_or_create("session-1")
        self.assertEqual(recovered.summary, "Compacted session.")
        self.assertEqual(provider.complete_calls, [])

    async def test_compact_reports_when_no_history_can_be_compacted(self) -> None:
        provider = ScriptedProvider(())
        compactor = RecordingCompactor(provider)
        loop = self._loop(provider, session_compactor=compactor)

        result = await _dispatch(loop, "/compact", "test", "chat-1", "session-1")

        self.assertIn("没有可压缩", result.content)
        self.assertEqual(len(compactor.compact_calls), 1)
        self.assertEqual(provider.complete_calls, [])

    async def test_memory_reads_with_a_bounded_output_and_does_not_call_the_provider(self) -> None:
        provider = ScriptedProvider(())
        memory_store = MemoryStore(self._temporary_directory.name)
        memory_store.write("A" * 24)
        router = CommandRouter(
            self._sessions,
            memory_store=memory_store,
            memory_output_limit=16,
        )
        loop = self._loop(provider, command_router=router)

        result = await _dispatch(loop, "/memory", "test", "chat-1", "session-1")

        self.assertTrue(result.content.startswith("A" * 16))
        self.assertIn("内容已截断", result.content)
        self.assertLessEqual(len(result.content), 32)
        self.assertEqual(provider.complete_calls, [])

    async def test_memory_reports_when_no_long_term_memory_exists(self) -> None:
        provider = ScriptedProvider(())
        router = CommandRouter(
            self._sessions,
            memory_store=MemoryStore(self._temporary_directory.name),
        )
        loop = self._loop(provider, command_router=router)

        result = await _dispatch(loop, "/memory", "test", "chat-1", "session-1")

        self.assertIn("还没有长期记忆", result.content)
        self.assertEqual(provider.complete_calls, [])

    async def test_stop_cancels_the_active_turn_without_storing_the_command(self) -> None:
        provider = BlockingProvider()
        loop = self._loop(provider)
        running_turn = asyncio.create_task(
            _dispatch(loop, "Long question.", "test", "chat-1", "session-1")
        )
        await asyncio.wait_for(provider.started.wait(), timeout=1)

        result = await _dispatch(loop, "/stop", "test", "chat-1", "session-1")

        self.assertIn("已请求停止", result.content)
        with self.assertRaises(asyncio.CancelledError):
            await running_turn
        self.assertTrue(provider.finished.is_set())
        self.assertEqual(
            self._sessions.get_or_create("session-1").messages,
            (HumanMessage(content="Long question."),),
        )

    async def test_stop_reports_when_no_turn_is_active(self) -> None:
        provider = ScriptedProvider(())
        loop = self._loop(provider)

        result = await _dispatch(loop, "/stop", "test", "chat-1", "session-1")

        self.assertIn("没有正在执行", result.content)
        self.assertEqual(provider.complete_calls, [])

    async def test_bus_stop_has_priority_over_a_running_turn(self) -> None:
        bus = MessageBus()
        provider = BlockingProvider()
        loop = self._loop(provider, message_bus=bus)
        worker = asyncio.create_task(loop.run())
        try:
            await bus.publish_inbound(
                InboundMessage("test", "chat-1", "sender-1", "session-1", "Long question.")
            )
            await asyncio.wait_for(provider.started.wait(), timeout=1)
            await bus.publish_inbound(
                InboundMessage("test", "chat-1", "sender-1", "session-1", "/stop")
            )
            outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        finally:
            await _cancel_task(self, worker)

        self.assertIn("已请求停止", outbound.content)
        self.assertTrue(provider.finished.is_set())

    async def test_command_reply_preserves_bus_routing_and_metadata(self) -> None:
        bus = MessageBus()
        provider = ScriptedProvider(())
        loop = self._loop(provider, message_bus=bus)
        inbound = InboundMessage(
            "fake",
            "chat-1",
            "sender-1",
            "session-1",
            "/help",
            {"request_id": "request-1"},
        )
        await bus.publish_inbound(inbound)
        worker = asyncio.create_task(loop.run())
        try:
            outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        finally:
            await _cancel_task(self, worker)

        self.assertEqual(outbound.channel, inbound.channel)
        self.assertEqual(outbound.chat_id, inbound.chat_id)
        self.assertEqual(outbound.sender_id, inbound.sender_id)
        self.assertEqual(outbound.session_id, inbound.session_id)
        self.assertEqual(outbound.metadata, inbound.metadata)
        self.assertEqual(provider.complete_calls, [])

    async def test_commands_do_not_create_memory_events(self) -> None:
        provider = ScriptedProvider(())
        store = MemoryStore(self._temporary_directory.name)
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            self._sessions,
            ContextBuilder(self._temporary_directory.name),
            memory_store=store,
            memory_consolidator=MemoryConsolidator(provider, store),
        )

        await _dispatch(loop, "/help", "test", "chat-1", "session-1")
        await _dispatch(loop, "/memory", "test", "chat-1", "session-1")

        self.assertEqual(store.read_events_after(0), ())
        self.assertEqual(self._sessions.get_or_create("session-1").messages, ())

    async def test_new_waits_for_a_running_turn_before_resetting_the_session(self) -> None:
        provider = BlockingProvider()
        loop = self._loop(provider)
        running_turn = asyncio.create_task(
            _dispatch(loop, "Question.", "test", "chat-1", "session-1")
        )
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        new_turn = asyncio.create_task(
            _dispatch(loop, "/new", "test", "chat-1", "session-1")
        )
        await asyncio.sleep(0)
        self.assertFalse(new_turn.done())

        provider.release.set()
        await running_turn
        result = await new_turn

        self.assertIn("已开始新的会话", result.content)
        self.assertEqual(self._sessions.get_or_create("session-1").messages, ())

    def _loop(
        self,
        provider: LLMProvider,
        *,
        message_bus: MessageBus | None = None,
        session_compactor: SessionCompactor | None = None,
        command_router: CommandRouter | None = None,
    ) -> AgentLoop:
        return AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry(),
            self._sessions,
            ContextBuilder(self._temporary_directory.name),
            message_bus=message_bus,
            session_compactor=session_compactor,
            command_router=command_router,
        )


async def _cancel_task(
    test_case: unittest.IsolatedAsyncioTestCase,
    task: asyncio.Task[object],
) -> None:
    task.cancel()
    with test_case.assertRaises(asyncio.CancelledError):
        await task


async def _dispatch(
    loop: AgentLoop,
    content: str,
    channel: str,
    chat_id: str,
    session_id: str,
    metadata: dict[str, object] | None = None,
) -> OutboundMessage:
    """Exercise AgentLoop's queue dispatch without reintroducing a public shortcut."""

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
