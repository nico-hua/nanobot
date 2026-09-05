"""Focused tests for Agent command routing and session controls."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Awaitable, Callable, Sequence

from nanobot.agent import (
    AgentLoop,
    AgentRunner,
    AgentRunResult,
    AgentRunSpec,
    CommandRouter,
    ContextBuilder,
)
from nanobot.bus import InboundMessage, MessageBus, OutboundMessage
from nanobot.memory import MemoryConsolidator, MemoryStore
from nanobot.providers import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
    ToolMessage,
)
from nanobot.session import GoalState, Session, SessionCompactor, SessionManager
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


class FailingProvider(LLMProvider):
    def __init__(self) -> None:
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
        raise RuntimeError("goal execution failed")

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        raise AssertionError("Command tests must not use streaming")


class GoalInjectionProvider(LLMProvider):
    def __init__(self) -> None:
        self.complete_calls: list[tuple[BaseMessage, ...]] = []

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
        tool_results = tuple(
            message for message in request if isinstance(message, ToolMessage)
        )
        if tool_results:
            label = tool_results[-1].content.removeprefix("wait: ")
            return LLMResponse(content=f"Goal {label} completed.")
        goal_message = next(
            message.content
            for message in request
            if isinstance(message, HumanMessage) and message.content.startswith("Goal ")
        )
        label = goal_message.removeprefix("Goal ")
        return LLMResponse(
            tool_calls=(
                ToolCallRequest(
                    id=f"wait-{label}",
                    name="wait",
                    arguments={"label": label},
                ),
            )
        )

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        raise AssertionError("Command tests must not use streaming")


class BlockingTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="wait",
            description="Wait for the test to release the tool.",
            parameters=(
                ToolParameter(
                    name="label",
                    description="Identifies the goal execution.",
                    type="string",
                    required=True,
                ),
            ),
        )
        self.started = {label: asyncio.Event() for label in ("one", "two")}
        self.release = {label: asyncio.Event() for label in ("one", "two")}

    async def execute(self, **arguments: object) -> ToolResult:
        label = arguments["label"]
        if not isinstance(label, str):
            raise TypeError("label must be a string")
        self.started[label].set()
        await self.release[label].wait()
        return ToolResult(content=f"wait: {label}")


class NamedTool(Tool):
    def __init__(self, name: str) -> None:
        super().__init__(name, f"Test tool {name}.")

    async def execute(self, **arguments: object) -> ToolResult:
        del arguments
        return ToolResult(content="unused")


class SpecRecordingRunner(AgentRunner):
    def __init__(self) -> None:
        self.specs: list[AgentRunSpec] = []

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.specs.append(spec)
        return AgentRunResult(
            content="Recorded.",
            messages=(*spec.messages, AIMessage(content="Recorded.")),
            tools_used=(),
            token_usage=None,
            stop_reason="stop",
        )


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


class RecordingMessageBus(MessageBus):
    def __init__(self) -> None:
        super().__init__()
        self.inbound_messages: list[InboundMessage] = []

    async def publish_inbound(self, message: InboundMessage) -> None:
        self.inbound_messages.append(message)
        await super().publish_inbound(message)


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

    async def test_blocks_goal_tools_by_agent_run_mode(self) -> None:
        runner = SpecRecordingRunner()
        registry = ToolRegistry(
            (
                NamedTool("create_goal"),
                NamedTool("update_goal"),
            )
        )
        loop = self._loop(
            ScriptedProvider(()),
            runner=runner,
            tool_registry=registry,
        )
        self._sessions.save(
            self._sessions.get_or_create("session-goal").with_goal_state(
                GoalState.create("Verify goal tool permissions.")
            )
        )

        await _dispatch(loop, "Normal turn.", "test", "chat-1", "session-normal")
        await _dispatch(
            loop,
            "Goal turn.",
            "test",
            "chat-2",
            "session-goal",
            {"source": "goal"},
        )

        self.assertEqual(runner.specs[0].blocked_tool_names, ("update_goal",))
        self.assertFalse(runner.specs[0].is_goal_mode)
        self.assertEqual(runner.specs[1].blocked_tool_names, ("create_goal",))
        self.assertTrue(runner.specs[1].is_goal_mode)

    async def test_new_clears_and_persists_the_current_session_without_a_provider_call(self) -> None:
        previous = self._sessions.get_or_create("session-1").with_messages(
            (HumanMessage(content="Earlier question."), AIMessage(content="Earlier answer."))
        ).with_summary("Earlier summary.", 2).with_goal_state(
            GoalState.create("A completed objective.").finish("completed")
        )
        self._sessions.save(previous)
        provider = ScriptedProvider(())
        loop = self._loop(provider)

        result = await _dispatch(loop, "  /NEW  ", "test", "chat-1", "session-1")
        recovered = SessionManager(self._temporary_directory.name).get_or_create("session-1")

        self.assertIn("已开始新的会话", result.content)
        self.assertEqual(recovered.messages, ())
        self.assertIsNone(recovered.summary)
        self.assertEqual(recovered.summary_until, 0)
        self.assertIsNone(recovered.goal_state)
        self.assertEqual(provider.complete_calls, [])

    async def test_new_preserves_an_active_goal_and_does_not_reset_the_session(self) -> None:
        active_goal = GoalState.create("Finish the migration.")
        previous = self._sessions.get_or_create("session-1").with_messages(
            (HumanMessage(content="Earlier question."),)
        ).with_goal_state(active_goal)
        self._sessions.save(previous)
        provider = ScriptedProvider(())
        loop = self._loop(provider)

        result = await _dispatch(loop, "/new", "test", "chat-1", "session-1")

        restored = self._sessions.get_or_create("session-1")
        self.assertIn("存在进行中的目标", result.content)
        self.assertIn("/goal stop", result.content)
        self.assertEqual(restored.messages, previous.messages)
        self.assertEqual(restored.goal_state, active_goal)
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

    async def test_goal_creates_persists_and_publishes_an_active_goal(self) -> None:
        previous = self._sessions.get_or_create("session-1").with_messages(
            (HumanMessage(content="Earlier question."),)
        )
        self._sessions.save(previous)
        bus = RecordingMessageBus()
        provider = ScriptedProvider(())
        loop = self._loop(provider, message_bus=bus)

        result = await _dispatch(
            loop,
            "  /goal   Finish the migration.  ",
            "test",
            "chat-1",
            "session-1",
        )

        restored = SessionManager(self._temporary_directory.name).get_or_create("session-1")
        self.assertIn("已创建目标", result.content)
        self.assertIn("已开始执行目标", result.content)
        self.assertIsNotNone(restored.goal_state)
        self.assertEqual(restored.goal_state.status, "active")
        self.assertEqual(restored.goal_state.objective, "Finish the migration.")
        self.assertEqual(restored.messages, previous.messages)
        self.assertEqual(len(bus.inbound_messages), 1)
        progress_message = bus.inbound_messages[0]
        self.assertEqual(progress_message.channel, "test")
        self.assertEqual(progress_message.chat_id, "chat-1")
        self.assertEqual(progress_message.sender_id, "test-sender")
        self.assertEqual(progress_message.session_id, "session-1")
        self.assertEqual(progress_message.metadata, {"source": "goal"})
        self.assertEqual(
            progress_message.content,
            "Start working on the current sustained goal.\n\n"
            "Goal:\n"
            "Finish the migration.\n\n"
            "Begin from the context saved in the current Session and use the available "
            "tools to make steady progress toward the goal.",
        )
        self.assertEqual(provider.complete_calls, [])

    async def test_goal_rejects_an_empty_objective(self) -> None:
        provider = ScriptedProvider(())
        loop = self._loop(provider)

        result = await _dispatch(loop, "/goal   ", "test", "chat-1", "session-1")

        self.assertEqual(
            result.content,
            "用法：/goal <目标描述>、/goal status 或 /goal stop",
        )
        self.assertIsNone(self._sessions.get_or_create("session-1").goal_state)
        self.assertEqual(provider.complete_calls, [])

    async def test_goal_status_returns_the_current_goal_without_running_the_agent(self) -> None:
        active_goal = GoalState.create("Finish the migration.")
        self._sessions.save(
            self._sessions.get_or_create("session-1").with_goal_state(active_goal)
        )
        bus = RecordingMessageBus()
        provider = ScriptedProvider(())
        loop = self._loop(provider, message_bus=bus)

        result = await _dispatch(loop, "/goal status", "test", "chat-1", "session-1")

        self.assertIn("当前目标状态：active", result.content)
        self.assertIn("Finish the migration.", result.content)
        self.assertEqual(self._sessions.get_or_create("session-1").goal_state, active_goal)
        self.assertEqual(bus.inbound_messages, [])
        self.assertEqual(provider.complete_calls, [])

    async def test_goal_stop_cancels_and_persists_the_current_goal_without_running_the_agent(self) -> None:
        active_goal = GoalState.create("Finish the migration.")
        self._sessions.save(
            self._sessions.get_or_create("session-1").with_goal_state(active_goal)
        )
        bus = RecordingMessageBus()
        provider = ScriptedProvider(())
        loop = self._loop(provider, message_bus=bus)
        loop._queue_goal_user_message(
            "session-1",
            InboundMessage("test", "chat-1", "sender-1", "session-1", "Pending input."),
        )
        loop._queue_goal_user_message(
            "session-2",
            InboundMessage("test", "chat-2", "sender-2", "session-2", "Keep this input."),
        )

        result = await _dispatch(loop, "/goal stop", "test", "chat-1", "session-1")

        restored = SessionManager(self._temporary_directory.name).get_or_create("session-1")
        self.assertIn("已停止目标", result.content)
        self.assertIsNotNone(restored.goal_state)
        self.assertEqual(restored.goal_state.status, "cancelled")
        self.assertIsNotNone(restored.goal_state.ended_at)
        self.assertEqual(bus.inbound_messages, [])
        self.assertEqual(provider.complete_calls, [])
        self.assertNotIn("session-1", loop._pending_user_messages)
        self.assertIn("session-2", loop._pending_user_messages)

    async def test_goal_status_and_stop_report_when_no_goal_exists(self) -> None:
        provider = ScriptedProvider(())
        loop = self._loop(provider)

        status_result = await _dispatch(loop, "/goal status", "test", "chat-1", "session-1")
        stop_result = await _dispatch(loop, "/goal stop", "test", "chat-1", "session-1")

        self.assertEqual(status_result.content, "当前会话没有目标。")
        self.assertEqual(stop_result.content, "当前会话没有目标。")
        self.assertEqual(provider.complete_calls, [])

    async def test_goal_progress_message_uses_the_saved_session_context(self) -> None:
        previous = self._sessions.get_or_create("session-1").with_messages(
            (
                HumanMessage(content="Earlier question."),
                AIMessage(content="Earlier answer."),
            )
        )
        self._sessions.save(previous)
        bus = RecordingMessageBus()
        provider = ScriptedProvider((LLMResponse(content="Goal progress."),))
        loop = self._loop(provider, message_bus=bus)

        await _dispatch(loop, "/goal Finish the migration.", "test", "chat-1", "session-1")
        progress_message = await bus.consume_inbound()
        result = await loop._dispatch_non_stop_message(progress_message, None)

        request_messages = provider.complete_calls[0]
        self.assertIn(previous.messages[0], request_messages)
        self.assertIn(previous.messages[1], request_messages)
        self.assertEqual(
            request_messages[-1],
            HumanMessage(content=progress_message.content),
        )
        self.assertEqual(result.content, "Goal progress.")
        self.assertEqual(result.metadata["source"], "goal")
        completed_goal = self._sessions.get_or_create("session-1").goal_state
        self.assertIsNotNone(completed_goal)
        self.assertEqual(completed_goal.status, "completed")
        self.assertIsNotNone(completed_goal.ended_at)
        self.assertFalse(loop._is_goal_mode("session-1"))

    async def test_goal_turn_failure_marks_the_active_goal_as_failed(self) -> None:
        active_goal = GoalState.create("Finish the migration.")
        self._sessions.save(
            self._sessions.get_or_create("session-1").with_goal_state(active_goal)
        )
        provider = FailingProvider()
        loop = self._loop(provider)

        with self.assertRaisesRegex(RuntimeError, "goal execution failed"):
            await _dispatch(
                loop,
                "Continue the goal.",
                "test",
                "chat-1",
                "session-1",
                {"source": "goal"},
            )

        failed_goal = self._sessions.get_or_create("session-1").goal_state
        self.assertIsNotNone(failed_goal)
        self.assertEqual(failed_goal.status, "failed")
        self.assertIsNotNone(failed_goal.ended_at)
        self.assertFalse(loop._is_goal_mode("session-1"))

    async def test_goal_commands_bypass_goal_mode_and_stop_cancels_execution(self) -> None:
        active_goal = GoalState.create("Finish the migration.")
        self._sessions.save(
            self._sessions.get_or_create("session-1").with_goal_state(active_goal)
        )
        bus = MessageBus()
        provider = BlockingProvider()
        loop = self._loop(provider, message_bus=bus)
        worker = asyncio.create_task(loop.run())
        try:
            await bus.publish_inbound(
                InboundMessage(
                    "test",
                    "chat-1",
                    "sender-1",
                    "session-1",
                    "Continue the goal.",
                    {"source": "goal"},
                )
            )
            await asyncio.wait_for(provider.started.wait(), timeout=1)
            self.assertTrue(loop._is_goal_mode("session-1"))

            await bus.publish_inbound(
                InboundMessage(
                    "test",
                    "chat-1",
                    "sender-1",
                    "session-1",
                    "/goal A replacement objective.",
                )
            )
            create_reply = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
            self.assertIn("Finish the migration.", create_reply.content)

            await bus.publish_inbound(
                InboundMessage(
                    "test",
                    "chat-1",
                    "sender-1",
                    "session-1",
                    "/goal status",
                )
            )
            status_reply = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
            self.assertIn("active", status_reply.content)

            await bus.publish_inbound(
                InboundMessage(
                    "test",
                    "chat-1",
                    "sender-1",
                    "session-1",
                    "/goal stop",
                )
            )
            stop_reply = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
            self.assertIn("Finish the migration.", stop_reply.content)
            await asyncio.wait_for(provider.finished.wait(), timeout=1)
            await asyncio.sleep(0)

            cancelled_goal = self._sessions.get_or_create("session-1").goal_state
            self.assertIsNotNone(cancelled_goal)
            self.assertEqual(cancelled_goal.status, "cancelled")
            self.assertFalse(loop._is_goal_mode("session-1"))
        finally:
            await _cancel_task(self, worker)

    async def test_goal_runner_injects_merged_user_messages_without_crossing_sessions(
        self,
    ) -> None:
        self._sessions.save(
            self._sessions.get_or_create("session-one").with_goal_state(
                GoalState.create("Finish goal one.")
            )
        )
        self._sessions.save(
            self._sessions.get_or_create("session-two").with_goal_state(
                GoalState.create("Finish goal two.")
            )
        )
        bus = MessageBus()
        provider = GoalInjectionProvider()
        tool = BlockingTool()
        loop = AgentLoop(
            AgentRunner(),
            provider,
            ToolRegistry((tool,)),
            self._sessions,
            ContextBuilder(self._temporary_directory.name),
            message_bus=bus,
        )
        worker = asyncio.create_task(loop.run())
        try:
            await bus.publish_inbound(
                InboundMessage(
                    "test",
                    "chat-one",
                    "sender-1",
                    "session-one",
                    "Goal one",
                    {"source": "goal"},
                )
            )
            await asyncio.wait_for(tool.started["one"].wait(), timeout=1)
            await bus.publish_inbound(
                InboundMessage(
                    "test",
                    "chat-two",
                    "sender-2",
                    "session-two",
                    "Goal two",
                    {"source": "goal"},
                )
            )
            await asyncio.wait_for(tool.started["two"].wait(), timeout=1)

            await bus.publish_inbound(
                InboundMessage(
                    "test",
                    "chat-one",
                    "sender-1",
                    "session-one",
                    "First update for one.",
                )
            )
            await bus.publish_inbound(
                InboundMessage(
                    "test",
                    "chat-one",
                    "sender-1",
                    "session-one",
                    "Second update for one.",
                )
            )
            await bus.publish_inbound(
                InboundMessage(
                    "test",
                    "chat-two",
                    "sender-2",
                    "session-two",
                    "Only update for two.",
                )
            )
            await asyncio.sleep(0)

            self.assertEqual(len(provider.complete_calls), 2)
            self.assertEqual(loop._pending_user_messages["session-one"].qsize(), 2)
            self.assertEqual(loop._pending_user_messages["session-two"].qsize(), 1)

            await bus.publish_inbound(
                InboundMessage(
                    "test",
                    "chat-one",
                    "sender-1",
                    "session-one",
                    "/goal status",
                )
            )
            status_reply = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
            self.assertIn("active", status_reply.content)
            self.assertEqual(loop._pending_user_messages["session-one"].qsize(), 2)

            await bus.publish_inbound(
                InboundMessage(
                    "test",
                    "chat-one",
                    "sender-1",
                    "session-one",
                    "/help",
                )
            )
            await asyncio.sleep(0)
            self.assertTrue(bus._outbound.empty())
            self.assertEqual(loop._pending_user_messages["session-one"].qsize(), 2)

            tool.release["one"].set()
            first_session_responses = (
                await asyncio.wait_for(bus.consume_outbound(), timeout=1),
                await asyncio.wait_for(bus.consume_outbound(), timeout=1),
            )
            self.assertTrue(
                any(
                    response.content == "Goal one completed."
                    for response in first_session_responses
                )
            )
            self.assertTrue(
                any("/help" in response.content for response in first_session_responses)
            )

            tool.release["two"].set()
            second_session_response = await asyncio.wait_for(
                bus.consume_outbound(),
                timeout=1,
            )
            self.assertEqual(second_session_response.content, "Goal two completed.")

            one_follow_up = next(
                request
                for request in provider.complete_calls
                if any(
                    isinstance(message, ToolMessage) and message.content == "wait: one"
                    for message in request
                )
            )
            two_follow_up = next(
                request
                for request in provider.complete_calls
                if any(
                    isinstance(message, ToolMessage) and message.content == "wait: two"
                    for message in request
                )
            )
            one_injection = next(
                message
                for message in one_follow_up
                if isinstance(message, HumanMessage)
                and message.content.startswith("[New user input during goal execution]")
            )
            two_injection = next(
                message
                for message in two_follow_up
                if isinstance(message, HumanMessage)
                and message.content.startswith("[New user input during goal execution]")
            )
            self.assertLess(
                one_injection.content.index("First update for one."),
                one_injection.content.index("Second update for one."),
            )
            self.assertNotIn("Only update for two.", one_injection.content)
            self.assertIn("Only update for two.", two_injection.content)
            self.assertNotIn("First update for one.", two_injection.content)
            self.assertIn("use the update_goal tool", one_injection.content)
            self.assertNotIn("/goal", one_injection.content)

            first_session = self._sessions.get_or_create("session-one")
            second_session = self._sessions.get_or_create("session-two")
            self.assertEqual(first_session.messages.count(one_injection), 1)
            self.assertEqual(second_session.messages.count(two_injection), 1)
            self.assertNotIn(HumanMessage(content="First update for one."), first_session.messages)
            self.assertNotIn(HumanMessage(content="Only update for two."), first_session.messages)
            self.assertNotIn("session-one", loop._pending_user_messages)
            self.assertNotIn("session-two", loop._pending_user_messages)
        finally:
            await _cancel_task(self, worker)

    async def test_goal_does_not_replace_an_active_goal(self) -> None:
        active_goal = GoalState.create("Keep the current objective.")
        self._sessions.save(
            self._sessions.get_or_create("session-1").with_goal_state(active_goal)
        )
        provider = ScriptedProvider(())
        loop = self._loop(provider)

        result = await _dispatch(
            loop,
            "/goal Replace it.",
            "test",
            "chat-1",
            "session-1",
        )

        restored = self._sessions.get_or_create("session-1")
        self.assertIn("已有进行中的目标", result.content)
        self.assertEqual(restored.goal_state, active_goal)
        self.assertEqual(provider.complete_calls, [])

    async def test_goal_replaces_a_terminal_goal(self) -> None:
        terminal_goal = GoalState.create("Old objective.").finish("failed")
        self._sessions.save(
            self._sessions.get_or_create("session-1").with_goal_state(terminal_goal)
        )
        provider = ScriptedProvider(())
        loop = self._loop(provider)

        await _dispatch(loop, "/goal New objective.", "test", "chat-1", "session-1")

        restored = self._sessions.get_or_create("session-1")
        self.assertIsNotNone(restored.goal_state)
        self.assertEqual(restored.goal_state.status, "active")
        self.assertEqual(restored.goal_state.objective, "New objective.")
        self.assertEqual(provider.complete_calls, [])

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
            (),
        )

    async def test_stop_reports_when_no_turn_is_active(self) -> None:
        provider = ScriptedProvider(())
        loop = self._loop(provider)
        loop._queue_goal_user_message(
            "session-1",
            InboundMessage("test", "chat-1", "sender-1", "session-1", "Pending input."),
        )
        loop._queue_goal_user_message(
            "session-2",
            InboundMessage("test", "chat-2", "sender-2", "session-2", "Keep this input."),
        )

        result = await _dispatch(loop, "/stop", "test", "chat-1", "session-1")

        self.assertIn("没有正在执行", result.content)
        self.assertEqual(provider.complete_calls, [])
        self.assertNotIn("session-1", loop._pending_user_messages)
        self.assertIn("session-2", loop._pending_user_messages)

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
        runner: AgentRunner | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> AgentLoop:
        return AgentLoop(
            runner or AgentRunner(),
            provider,
            tool_registry or ToolRegistry(),
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
