"""Focused lifecycle and command tests for background subagent tasks."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from nanobot.agent import (
    AgentLoop,
    AgentRunResult,
    AgentRunner,
    CommandRouter,
    ContextBuilder,
)
from nanobot.bus import InboundMessage, MessageBus
from nanobot.providers import AIMessage, BaseMessage, LLMProvider, LLMResponse
from nanobot.session import SessionManager
from nanobot.subagent import SubagentManager
from nanobot.tools import RequestContext, Tool, ToolContext, ToolRegistry


class UnusedProvider(LLMProvider):
    async def complete(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        del messages, tools, max_tokens, temperature
        raise AssertionError("Recording runner must handle subagent execution")

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        raise AssertionError("Lifecycle tests do not use streaming")


class BlockingRunner(AgentRunner):
    def __init__(self, result: AgentRunResult | None = None) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self._result = result or _result("Completed child task.")

    async def run(self, spec: object) -> AgentRunResult:
        del spec
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return self._result


class FailingRunner(AgentRunner):
    async def run(self, spec: object) -> AgentRunResult:
        del spec
        raise RuntimeError("expected test failure")


class SubagentLifecycleTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._workspace = Path(self._temporary_directory.name)
        self._bus = MessageBus()
        self._provider = UnusedProvider()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_task_transitions_from_pending_to_running_then_completed(self) -> None:
        runner = BlockingRunner()
        manager = self._manager(runner)

        task_id = manager.start_background("Investigate.", request_context=_context())
        self.assertEqual(manager.get_task(task_id).status, "pending")

        await asyncio.wait_for(runner.started.wait(), timeout=1)
        self.assertEqual(manager.get_task(task_id).status, "running")
        runner.release.set()
        await asyncio.wait_for(self._bus.consume_inbound(), timeout=1)

        task = manager.get_task(task_id)
        self.assertEqual(task.status, "completed")
        self.assertIsNotNone(task.finished_at)
        self.assertEqual(task.result_summary, "Completed child task.")
        self.assertFalse(manager.cancel_background(task_id, session_key="session-a"))

    async def test_failed_cancelled_and_timed_out_tasks_reach_their_terminal_state(self) -> None:
        failed_manager = self._manager(FailingRunner())
        failed_id = failed_manager.start_background("Fail.", request_context=_context())
        failed_message = await asyncio.wait_for(self._bus.consume_inbound(), timeout=1)
        self.assertEqual(failed_message.metadata["subagent_status"], "failed")
        self.assertEqual(failed_manager.get_task(failed_id).status, "failed")

        cancelled_runner = BlockingRunner()
        cancelled_manager = self._manager(cancelled_runner)
        cancelled_id = cancelled_manager.start_background(
            "Cancel.",
            request_context=_context(),
        )
        await asyncio.wait_for(cancelled_runner.started.wait(), timeout=1)
        self.assertTrue(
            cancelled_manager.cancel_background(cancelled_id, session_key="session-a")
        )
        await asyncio.wait_for(cancelled_runner.cancelled.wait(), timeout=1)
        self.assertEqual(cancelled_manager.get_task(cancelled_id).status, "cancelled")
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(self._bus.consume_inbound(), timeout=0.01)

        timeout_runner = BlockingRunner()
        timeout_manager = self._manager(timeout_runner, timeout=0.01)
        timeout_id = timeout_manager.start_background("Timeout.", request_context=_context())
        timeout_message = await asyncio.wait_for(self._bus.consume_inbound(), timeout=1)
        self.assertEqual(timeout_message.metadata["subagent_status"], "timeout")
        self.assertEqual(timeout_manager.get_task(timeout_id).status, "timeout")
        self.assertTrue(timeout_runner.cancelled.is_set())

    async def test_cancelled_task_does_not_publish_a_later_success_result(self) -> None:
        runner = BlockingRunner()
        manager = self._manager(runner)
        task_id = manager.start_background("Cancel me.", request_context=_context())
        await asyncio.wait_for(runner.started.wait(), timeout=1)

        self.assertTrue(manager.cancel_background(task_id, session_key="session-a"))
        runner.release.set()
        await asyncio.wait_for(runner.cancelled.wait(), timeout=1)
        await asyncio.sleep(0)

        self.assertEqual(manager.get_task(task_id).status, "cancelled")
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(self._bus.consume_inbound(), timeout=0.01)

    async def test_limit_and_close_cancel_all_active_tasks(self) -> None:
        runner = BlockingRunner()
        manager = self._manager(runner, max_background_tasks=1)
        first_id = manager.start_background("First.", request_context=_context())
        await asyncio.wait_for(runner.started.wait(), timeout=1)

        with self.assertRaisesRegex(RuntimeError, "limit"):
            manager.start_background("Second.", request_context=_context())

        await manager.close()
        await manager.close()

        self.assertTrue(runner.cancelled.is_set())
        self.assertEqual(manager.get_task(first_id).status, "cancelled")
        self.assertEqual(manager.running_tasks, ())

    async def test_subagent_commands_are_session_scoped_and_do_not_cancel_final_tasks(self) -> None:
        runner = BlockingRunner()
        manager = self._manager(runner)
        sessions = SessionManager(self._workspace)
        router = CommandRouter(sessions, subagent_manager=manager)
        task_id = manager.start_background("Inspect.", request_context=_context())
        await asyncio.wait_for(runner.started.wait(), timeout=1)

        listed = await _route(router, sessions, "session-a", "/subagents")
        status = await _route(router, sessions, "session-a", f"/subagents status {task_id}")
        foreign_status = await _route(
            router,
            sessions,
            "session-b",
            f"/subagents status {task_id}",
        )
        foreign_cancel = await _route(
            router,
            sessions,
            "session-b",
            f"/subagents cancel {task_id}",
        )
        cancelled = await _route(
            router,
            sessions,
            "session-a",
            f"/subagents cancel {task_id}",
        )
        await asyncio.wait_for(runner.cancelled.wait(), timeout=1)
        repeated = await _route(
            router,
            sessions,
            "session-a",
            f"/subagents cancel {task_id}",
        )

        self.assertIn(task_id, listed)
        self.assertIn("running", status)
        self.assertIn("未找到", foreign_status)
        self.assertIn("未找到", foreign_cancel)
        self.assertIn("已请求取消", cancelled)
        self.assertIn("不能再次取消", repeated)
        self.assertEqual(sessions.get_or_create("session-a").messages, ())

    async def test_agent_loop_registers_subagent_commands_without_calling_the_provider(self) -> None:
        runner = BlockingRunner()
        manager = self._manager(runner)
        sessions = SessionManager(self._workspace)
        loop = AgentLoop(
            AgentRunner(),
            self._provider,
            ToolRegistry(),
            sessions,
            ContextBuilder(self._workspace),
            subagent_manager=manager,
        )
        task_id = manager.start_background("Inspect.", request_context=_context())
        await asyncio.wait_for(runner.started.wait(), timeout=1)
        inbound = InboundMessage(
            "test",
            "chat-1",
            "sender-1",
            "session-a",
            "/subagents",
        )

        reply = await loop._dispatch_non_stop_message(
            inbound,
            loop._command_router.parse(inbound.content),
        )

        self.assertIn(task_id, reply.content)
        await loop.close()

    def _manager(
        self,
        runner: AgentRunner,
        *,
        max_background_tasks: int = 4,
        timeout: float = 10,
    ) -> SubagentManager:
        return SubagentManager(
            runner,
            self._provider,
            ContextBuilder(self._workspace),
            ToolContext(workspace=self._workspace),
            message_bus=self._bus,
            max_background_tasks=max_background_tasks,
            background_timeout_seconds=timeout,
        )


async def _route(
    router: CommandRouter,
    sessions: SessionManager,
    session_key: str,
    content: str,
) -> str:
    reply = await router.route(
        InboundMessage("test", "chat-1", "sender-1", session_key, content),
        session_key,
        sessions.get_or_create(session_key),
    )
    if reply is None:
        raise AssertionError("Expected a command reply")
    return reply.content


def _context() -> RequestContext:
    return RequestContext(
        session_key="session-a",
        channel="test",
        chat_id="chat-1",
        sender_id="sender-1",
    )


def _result(content: str) -> AgentRunResult:
    return AgentRunResult(
        content=content,
        messages=(AIMessage(content=content),),
        tools_used=(),
        token_usage=None,
        stop_reason="stop",
    )
