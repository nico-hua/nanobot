"""Focused coverage for goal continuations after an iteration boundary."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from nanobot.agent import AgentLoop, AgentRunner, ContextBuilder
from nanobot.agent.loop import _is_goal_continuation_message
from nanobot.bus import InboundMessage, MessageBus
from nanobot.providers import (
    AIMessage,
    BaseMessage,
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
    ToolMessage,
)
from nanobot.session import GoalState, SessionManager
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
        try:
            return next(self._responses)
        except StopIteration as error:
            raise AssertionError("Provider received an unexpected completion request") from error

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        raise AssertionError("Goal continuation tests must not use streaming")


class RecordingTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="record",
            description="Record one test value.",
            parameters=(
                ToolParameter(
                    name="value",
                    description="The test value to record.",
                    type="string",
                    required=True,
                ),
            ),
        )
        self.calls: list[str] = []

    async def execute(self, **arguments: Any) -> ToolResult:
        value = arguments["value"]
        if not isinstance(value, str):
            raise TypeError("value must be a string")
        self.calls.append(value)
        return ToolResult(content=f"recorded: {value}")


class RecordingMessageBus(MessageBus):
    def __init__(self) -> None:
        super().__init__()
        self.published_inbound: list[InboundMessage] = []

    async def publish_inbound(self, message: InboundMessage) -> None:
        self.published_inbound.append(message)
        await super().publish_inbound(message)


def _tool_call(call_id: str, value: str) -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id,
        name="record",
        arguments={"value": value},
    )


def _goal_message(session_key: str, *, continuation: bool = False) -> InboundMessage:
    return InboundMessage(
        channel="test",
        chat_id=f"chat-{session_key}",
        sender_id="sender-1",
        session_id=session_key,
        content="Continue the active goal.",
        metadata={
            "source": "goal",
            **({"goal_continuation": True} if continuation else {}),
        },
    )


class GoalContinuationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._sessions = SessionManager(self._temporary_directory.name)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_continuation_requires_the_explicit_internal_marker(self) -> None:
        initial_goal = _goal_message("session-1")
        continuation = _goal_message("session-1", continuation=True)
        malformed = InboundMessage(
            channel="test",
            chat_id="chat-session-1",
            sender_id="sender-1",
            session_id="session-1",
            content="Continue the active goal.",
            metadata={"source": "goal", "goal_continuation": "true"},
        )

        self.assertFalse(_is_goal_continuation_message(initial_goal))
        self.assertTrue(_is_goal_continuation_message(continuation))
        self.assertFalse(_is_goal_continuation_message(malformed))

    async def test_max_iterations_persists_tool_batch_and_queues_one_continuation(
        self,
    ) -> None:
        provider = ScriptedProvider(
            (
                LLMResponse(tool_calls=(_tool_call("call-1", "first"),)),
                LLMResponse(content="Goal completed."),
            )
        )
        tool = RecordingTool()
        bus = RecordingMessageBus()
        session_key = "session-1"
        self._activate_goal(session_key)
        loop = self._loop(
            provider,
            ToolRegistry((tool,)),
            bus,
            max_goal_continuations=2,
        )
        worker = asyncio.create_task(loop.run())
        try:
            await bus.publish_inbound(_goal_message(session_key))
            outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
            await asyncio.sleep(0)

            self.assertEqual(outbound.content, "Goal completed.")
            self.assertTrue(bus._outbound.empty())
            continuations = [
                message
                for message in bus.published_inbound
                if message.metadata.get("goal_continuation") is True
            ]
            self.assertEqual(len(continuations), 1)
            continuation = continuations[0]
            self.assertEqual(continuation.session_id, session_key)
            self.assertEqual(continuation.metadata["source"], "goal")
            self.assertEqual(continuation.channel, "test")
            self.assertEqual(continuation.chat_id, f"chat-{session_key}")
            self.assertIn("Continue executing", continuation.content)
            self.assertIn("Finish the migration.", continuation.content)
            self.assertNotIn("Start working", continuation.content)

            saved = self._sessions.get_or_create(session_key)
            self.assertIsNotNone(saved.goal_state)
            self.assertEqual(saved.goal_state.status, "completed")
            self.assertEqual(saved.goal_state.continuation_count, 1)
            tool_call_message = next(
                message
                for message in saved.messages
                if isinstance(message, AIMessage) and message.tool_calls
            )
            tool_result_index = saved.messages.index(
                ToolMessage(content="recorded: first", tool_call_id="call-1")
            )
            self.assertLess(saved.messages.index(tool_call_message), tool_result_index)
            self.assertEqual(tool.calls, ["first"])
        finally:
            await _cancel_task(self, worker)

    async def test_terminal_goal_ignores_stale_continuations(self) -> None:
        provider = ScriptedProvider(())
        bus = RecordingMessageBus()
        loop = self._loop(provider, ToolRegistry(), bus)
        worker = asyncio.create_task(loop.run())
        try:
            for status in ("completed", "cancelled", "failed"):
                session_key = f"session-{status}"
                self._sessions.save(
                    self._sessions.get_or_create(session_key).with_goal_state(
                        GoalState.create("A finished goal.").finish(status)
                    )
                )
                await bus.publish_inbound(_goal_message(session_key, continuation=True))

            await asyncio.sleep(0)
            await asyncio.sleep(0)

            self.assertEqual(provider.complete_calls, [])
            self.assertTrue(bus._outbound.empty())
        finally:
            await _cancel_task(self, worker)

    async def test_empty_goal_result_marks_the_goal_failed(self) -> None:
        provider = ScriptedProvider((LLMResponse(content="  "),))
        bus = RecordingMessageBus()
        session_key = "session-empty-result"
        self._activate_goal(session_key)
        loop = self._loop(provider, ToolRegistry(), bus)

        result = await loop._dispatch_non_stop_message(
            _goal_message(session_key),
            None,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.content, "  ")
        goal = self._sessions.get_or_create(session_key).goal_state
        self.assertIsNotNone(goal)
        self.assertEqual(goal.status, "failed")

    async def test_new_goal_starts_after_a_terminal_goal(self) -> None:
        provider = ScriptedProvider((LLMResponse(content="New goal completed."),))
        bus = RecordingMessageBus()
        session_key = "session-1"
        self._sessions.save(
            self._sessions.get_or_create(session_key).with_goal_state(
                GoalState.create("Old goal.").finish("failed")
            )
        )
        loop = self._loop(provider, ToolRegistry(), bus)
        worker = asyncio.create_task(loop.run())
        try:
            await bus.publish_inbound(
                InboundMessage(
                    channel="test",
                    chat_id=f"chat-{session_key}",
                    sender_id="sender-1",
                    session_id=session_key,
                    content="/goal New goal.",
                )
            )
            creation_reply = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
            completion_reply = await asyncio.wait_for(bus.consume_outbound(), timeout=1)

            self.assertIn("New goal.", creation_reply.content)
            self.assertEqual(completion_reply.content, "New goal completed.")
            self.assertEqual(len(provider.complete_calls), 1)
            goal = self._sessions.get_or_create(session_key).goal_state
            self.assertIsNotNone(goal)
            self.assertEqual(goal.objective, "New goal.")
            self.assertEqual(goal.status, "completed")
        finally:
            await _cancel_task(self, worker)

    async def test_non_goal_iteration_limit_does_not_queue_a_continuation(self) -> None:
        provider = ScriptedProvider(
            (LLMResponse(tool_calls=(_tool_call("call-1", "ordinary"),)),)
        )
        bus = RecordingMessageBus()
        loop = self._loop(provider, ToolRegistry((RecordingTool(),)), bus)
        inbound = InboundMessage(
            channel="test",
            chat_id="chat-ordinary",
            sender_id="sender-1",
            session_id="session-ordinary",
            content="Use the tool.",
        )

        result = await loop._dispatch_non_stop_message(inbound, None)

        self.assertIsNotNone(result)
        self.assertTrue(result.content)
        self.assertEqual(bus.published_inbound, [])
        self.assertIsNone(self._sessions.get_or_create("session-ordinary").goal_state)

    async def test_goal_stop_prevents_a_queued_continuation_from_running(self) -> None:
        provider = ScriptedProvider(
            (LLMResponse(tool_calls=(_tool_call("call-1", "first"),)),)
        )
        bus = RecordingMessageBus()
        session_key = "session-1"
        self._activate_goal(session_key)
        loop = self._loop(provider, ToolRegistry((RecordingTool(),)), bus)

        first_result = await loop._dispatch_non_stop_message(
            _goal_message(session_key),
            None,
        )
        continuation = await bus.consume_inbound()
        stop_message = InboundMessage(
            channel="test",
            chat_id=f"chat-{session_key}",
            sender_id="sender-1",
            session_id=session_key,
            content="/goal stop",
        )

        stop_result = await loop._dispatch_non_stop_message(
            stop_message,
            loop._command_router.parse(stop_message.content),
        )
        skipped_result = await loop._dispatch_non_stop_message(continuation, None)

        self.assertIsNone(first_result)
        self.assertIsNotNone(stop_result)
        self.assertIn("Finish the migration.", stop_result.content)
        self.assertIsNone(skipped_result)
        self.assertEqual(len(provider.complete_calls), 1)
        goal = self._sessions.get_or_create(session_key).goal_state
        self.assertIsNotNone(goal)
        self.assertEqual(goal.status, "cancelled")

    async def test_continuation_limit_marks_goal_failed_without_queuing_another_turn(
        self,
    ) -> None:
        provider = ScriptedProvider(
            (
                LLMResponse(tool_calls=(_tool_call("call-1", "first"),)),
                LLMResponse(tool_calls=(_tool_call("call-2", "second"),)),
            )
        )
        bus = RecordingMessageBus()
        session_key = "session-1"
        self._activate_goal(session_key)
        loop = self._loop(
            provider,
            ToolRegistry((RecordingTool(),)),
            bus,
            max_goal_continuations=1,
        )
        worker = asyncio.create_task(loop.run())
        try:
            await bus.publish_inbound(_goal_message(session_key))
            outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
            await asyncio.sleep(0)

            self.assertTrue(outbound.content)
            continuations = [
                message
                for message in bus.published_inbound
                if message.metadata.get("goal_continuation") is True
            ]
            self.assertEqual(len(continuations), 1)
            goal = self._sessions.get_or_create(session_key).goal_state
            self.assertIsNotNone(goal)
            self.assertEqual(goal.status, "failed")
            self.assertEqual(goal.continuation_count, 1)
            self.assertEqual(len(provider.complete_calls), 2)
        finally:
            await _cancel_task(self, worker)

    async def test_continuations_are_counted_independently_per_session(self) -> None:
        provider = ScriptedProvider(
            (
                LLMResponse(tool_calls=(_tool_call("call-one", "one"),)),
                LLMResponse(tool_calls=(_tool_call("call-two", "two"),)),
            )
        )
        bus = RecordingMessageBus()
        loop = self._loop(provider, ToolRegistry((RecordingTool(),)), bus)
        self._activate_goal("session-one")
        self._activate_goal("session-two")

        first = await loop._dispatch_non_stop_message(
            _goal_message("session-one"),
            None,
        )
        second = await loop._dispatch_non_stop_message(
            _goal_message("session-two"),
            None,
        )

        self.assertIsNone(first)
        self.assertIsNone(second)
        continuations = [
            message
            for message in bus.published_inbound
            if message.metadata.get("goal_continuation") is True
        ]
        self.assertEqual(
            tuple(message.session_id for message in continuations),
            ("session-one", "session-two"),
        )
        self.assertEqual(
            self._sessions.get_or_create("session-one").goal_state.continuation_count,
            1,
        )
        self.assertEqual(
            self._sessions.get_or_create("session-two").goal_state.continuation_count,
            1,
        )

    def _activate_goal(self, session_key: str) -> None:
        self._sessions.save(
            self._sessions.get_or_create(session_key).with_goal_state(
                GoalState.create("Finish the migration.")
            )
        )

    def _loop(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        message_bus: MessageBus,
        *,
        max_goal_continuations: int = 10,
    ) -> AgentLoop:
        return AgentLoop(
            AgentRunner(),
            provider,
            registry,
            self._sessions,
            ContextBuilder(self._temporary_directory.name),
            message_bus=message_bus,
            max_iterations=1,
            max_goal_continuations=max_goal_continuations,
        )


async def _cancel_task(
    test_case: unittest.IsolatedAsyncioTestCase,
    task: asyncio.Task[object],
) -> None:
    task.cancel()
    with test_case.assertRaises(asyncio.CancelledError):
        await task
