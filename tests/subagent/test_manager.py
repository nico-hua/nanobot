"""Focused tests for the minimal isolated SubagentManager."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from nanobot.agent import (
    AgentRunResult,
    AgentRunSpec,
    AgentRunner,
    AgentRunnerError,
    ContextBuilder,
)
from nanobot.bus import MessageBus
from nanobot.providers import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    LLMProvider,
    LLMResponse,
    SystemMessage,
)
from nanobot.subagent import SubagentManager
from nanobot.tools import (
    Tool,
    ToolContext,
    ToolLoader,
    ToolRegistry,
    ToolResult,
    get_request_context,
)


class UnusedProvider(LLMProvider):
    async def complete(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        del messages, tools, max_tokens, temperature
        raise AssertionError("The recording subagent runner does not call the provider")

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        raise AssertionError("SubagentManager does not use streaming")


class RecordingProvider(LLMProvider):
    def __init__(self, response: LLMResponse) -> None:
        self._response = response
        self.requests: list[tuple[tuple[BaseMessage, ...], tuple[Tool, ...]]] = []

    async def complete(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        del max_tokens, temperature
        self.requests.append((tuple(messages), tuple(tools or ())))
        return self._response

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        raise AssertionError("SubagentManager does not use streaming")


class RecordingRunner(AgentRunner):
    def __init__(self, result: AgentRunResult) -> None:
        self.result = result
        self.specs: list[AgentRunSpec] = []

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        self.specs.append(spec)
        return self.result


class FailingRunner(AgentRunner):
    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        del spec
        raise AgentRunnerError("expected subagent failure")


class RuntimeRecordingRunner(AgentRunner):
    def __init__(self, result: AgentRunResult) -> None:
        self.result = result
        self.request_context = None

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        del spec
        self.request_context = get_request_context()
        return self.result


class BlockingRunner(AgentRunner):
    def __init__(self, result: AgentRunResult) -> None:
        self.result = result
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        del spec
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return self.result


class SpawnTool(Tool):
    def __init__(self) -> None:
        super().__init__("spawn", "Create another subagent.")

    async def execute(self, **arguments: object) -> ToolResult:
        del arguments
        return ToolResult(content="spawned")


class SubagentManagerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._workspace = Path(self._temporary_directory.name)
        self._provider = UnusedProvider()
        self._context_builder = ContextBuilder(self._workspace)
        self._tool_context = ToolContext(workspace=self._workspace)
        self._main_tool_registry = ToolRegistry()
        ToolLoader().load(self._main_tool_registry, self._tool_context)
        self._main_tool_registry.register(SpawnTool())

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_builds_the_subagent_prompt_with_only_the_task_message(self) -> None:
        provider = RecordingProvider(LLMResponse(content="Completed.", finish_reason="stop"))
        manager = self._manager(AgentRunner(), provider)

        result = await manager.run("Review the workspace.")

        self.assertTrue(result.success)
        self.assertEqual(result.agent_result.content, "Completed.")
        self.assertEqual(
            provider.requests[0][0],
            (
                SystemMessage(
                    content=self._context_builder.build_subagent_system_prompt()
                ),
                HumanMessage(content="Review the workspace."),
            ),
        )
        self.assertEqual(
            tuple(tool.name for tool in provider.requests[0][1]),
            tuple(
                tool.name
                for tool in self._main_tool_registry.tools
                if tool.name not in {"spawn", "create_goal", "update_goal"}
            ),
        )

    async def test_does_not_include_parent_history(self) -> None:
        runner = RecordingRunner(_agent_result("Completed."))
        manager = self._manager(runner)
        parent_history = (
            SystemMessage(content="Parent system prompt."),
            HumanMessage(content="A parent-only detail."),
            AIMessage(content="Parent answer."),
        )

        await manager.run("Independent task.")

        self.assertEqual(len(runner.specs[0].messages), 2)
        self.assertTrue(
            all(
                message not in runner.specs[0].messages
                for message in parent_history
            )
        )

    async def test_uses_an_independent_registry_without_spawn(self) -> None:
        runner = RecordingRunner(_agent_result("Completed."))
        manager = self._manager(runner)

        await manager.run("First task.")
        await manager.run("Second task.")

        first_registry, second_registry = (
            runner.specs[0].tool_registry,
            runner.specs[1].tool_registry,
        )
        self.assertIsNot(first_registry, self._main_tool_registry)
        self.assertIsNot(second_registry, self._main_tool_registry)
        self.assertIs(first_registry, second_registry)
        self.assertFalse(first_registry.has("spawn"))
        self.assertEqual(
            runner.specs[0].blocked_tool_names,
            ("spawn", "create_goal", "update_goal"),
        )
        self.assertEqual(
            runner.specs[1].blocked_tool_names,
            ("spawn", "create_goal", "update_goal"),
        )

    async def test_binds_the_request_context_for_child_tools(self) -> None:
        runner = RuntimeRecordingRunner(_agent_result("Completed."))
        manager = self._manager(runner)
        request_context = _request_context()

        await manager.run("Use the current runtime.", request_context=request_context)

        self.assertIs(runner.request_context, request_context)

    async def test_loads_workspace_tools_into_the_child_registry(self) -> None:
        runner = RecordingRunner(_agent_result("Completed."))
        manager = self._manager(runner)

        await manager.run("Inspect the workspace.")

        read_file = runner.specs[0].tool_registry.get("read_file")
        self.assertIsNotNone(read_file)
        self.assertEqual(getattr(read_file, "workspace", None), self._workspace.resolve())

    async def test_returns_a_clear_error_when_the_runner_fails(self) -> None:
        manager = self._manager(FailingRunner())

        result = await manager.run("Fail safely.")

        self.assertFalse(result.success)
        self.assertIsNone(result.agent_result)
        self.assertEqual(result.error, "Subagent could not complete the task.")

    async def test_background_task_publishes_its_result_to_the_original_route(self) -> None:
        bus = MessageBus()
        runner = BlockingRunner(_agent_result("Child result."))
        manager = self._manager(runner, message_bus=bus)
        request_context = _request_context(
            metadata={
                "qq_chat_type": "c2c",
                "message_id": "origin-message-1",
            }
        )

        task_id = manager.start_background(
            "Investigate later.",
            request_context=request_context,
        )

        await asyncio.wait_for(runner.started.wait(), timeout=1)
        self.assertEqual(len(manager.running_tasks), 1)
        source = manager.running_tasks[0]
        self.assertEqual(source.task_id, task_id)
        self.assertEqual(source.parent_session_key, request_context.session_key)
        self.assertEqual(source.channel, request_context.channel)
        self.assertEqual(source.chat_id, request_context.chat_id)
        self.assertEqual(source.sender_id, request_context.sender_id)
        self.assertEqual(source.metadata, request_context.metadata)

        runner.release.set()
        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=1)
        await asyncio.sleep(0)

        self.assertEqual(inbound.channel, request_context.channel)
        self.assertEqual(inbound.chat_id, request_context.chat_id)
        self.assertEqual(inbound.sender_id, request_context.sender_id)
        self.assertEqual(inbound.session_id, request_context.session_key)
        self.assertIn("Child result.", inbound.content)
        self.assertEqual(
            inbound.metadata,
            {
                "qq_chat_type": "c2c",
                "message_id": "origin-message-1",
                "source": "subagent",
                "task_id": task_id,
                "parent_session_key": request_context.session_key,
                "subagent_status": "completed",
            },
        )
        self.assertEqual(manager.running_tasks, ())
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(bus.consume_inbound(), timeout=0.01)

    async def test_background_task_failure_publishes_a_clear_internal_message(self) -> None:
        bus = MessageBus()
        manager = self._manager(FailingRunner(), message_bus=bus)
        request_context = _request_context()

        task_id = manager.start_background("Fail safely.", request_context=request_context)
        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=1)

        self.assertEqual(inbound.sender_id, request_context.sender_id)
        self.assertIn("could not complete", inbound.content)
        self.assertEqual(inbound.metadata["task_id"], task_id)
        self.assertEqual(inbound.metadata["subagent_status"], "failed")

    async def test_close_cancels_background_tasks_without_publishing_a_result(self) -> None:
        bus = MessageBus()
        runner = BlockingRunner(_agent_result("unused"))
        manager = self._manager(runner, message_bus=bus)

        manager.start_background("Wait forever.", request_context=_request_context())
        await asyncio.wait_for(runner.started.wait(), timeout=1)
        await manager.close()
        await manager.close()

        self.assertTrue(runner.cancelled.is_set())
        self.assertEqual(manager.running_tasks, ())
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(bus.consume_inbound(), timeout=0.01)

    def _manager(
        self,
        runner: AgentRunner,
        provider: LLMProvider | None = None,
        message_bus: MessageBus | None = None,
    ) -> SubagentManager:
        return SubagentManager(
            runner,
            provider or self._provider,
            self._context_builder,
            self._tool_context,
            message_bus=message_bus,
        )


def _agent_result(content: str) -> AgentRunResult:
    return AgentRunResult(
        content=content,
        messages=(AIMessage(content=content),),
        tools_used=(),
        token_usage=None,
        stop_reason="stop",
    )


def _request_context(metadata: dict[str, str] | None = None):
    from nanobot.tools import RequestContext

    return RequestContext(
        session_key="session-1",
        channel="fake",
        chat_id="chat-1",
        sender_id="sender-1",
        metadata=metadata or {},
    )
