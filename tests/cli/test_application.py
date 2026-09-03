"""Focused tests for long-running Application assembly and shutdown."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from nanobot.agent import AgentRunner, ContextBuilder
from nanobot.bus import MessageBus
from nanobot.channels import BaseChannel, ChannelManager, FakeChannel
from nanobot.cli import Application
from nanobot.config import MCPServerConfig, NanobotConfig, ProviderConfig
from nanobot.cron import CronCallback, CronService
from nanobot.memory import MemoryConsolidator
from nanobot.providers import BaseMessage, LLMProvider, LLMResponse
from nanobot.tools import Tool, ToolContext, ToolLoader, ToolRegistry
from tests.tools.fakes import WeatherTool


class FakeProvider(LLMProvider):
    async def complete(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        del messages, tools, max_tokens, temperature
        return LLMResponse(content="unused")

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        raise AssertionError("Application tests do not use streaming")


class NoopToolLoader(ToolLoader):
    def __init__(self) -> None:
        self.contexts: list[ToolContext] = []

    def load(self, registry: ToolRegistry, context: ToolContext) -> tuple[str, ...]:
        del registry
        self.contexts.append(context)
        return ()


class FakeMCPProvider:
    def __init__(self, registry: ToolRegistry, servers: Mapping[str, MCPServerConfig], events: list[str]) -> None:
        self.registry = registry
        self.servers = servers
        self.events = events
        self.connected = False
        self.closed = False

    async def connect_all(self) -> tuple[Any, ...]:
        self.events.append("mcp.connect")
        self.registry.register(WeatherTool())
        self.connected = True
        return ()

    async def close(self) -> None:
        self.events.append("mcp.close")
        self.closed = True


class RecordingLoop:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.started = asyncio.Event()
        self.message_bus: MessageBus | None = None
        self._finish = asyncio.Event()
        self._error: Exception | None = None

    async def run(self) -> None:
        self.events.append("loop.start")
        self.started.set()
        try:
            await self._finish.wait()
            if self._error is not None:
                raise self._error
        finally:
            self.events.append("loop.close")

    def fail(self, error: Exception) -> None:
        self._error = error
        self._finish.set()

    def finish(self) -> None:
        self._finish.set()

    async def close(self) -> None:
        self.events.append("loop.background.close")


class RecordingChannel(FakeChannel):
    def __init__(self, name: str, message_bus: MessageBus, events: list[str]) -> None:
        super().__init__(name, message_bus)
        self.events = events

    async def start(self) -> None:
        self.events.append("channel.start")
        await super().start()

    async def stop(self) -> None:
        self.events.append("channel.close")
        await super().stop()


class FailingChannel(RecordingChannel):
    async def start(self) -> None:
        await super().start()
        await asyncio.sleep(0)
        raise RuntimeError("channel unavailable")


class FakeChannelManager:
    """A ChannelManager double with one controllable long-running task."""

    def __init__(
        self,
        message_bus: MessageBus,
        channels: tuple[BaseChannel, ...],
        events: list[str],
        *,
        start_error: Exception | None = None,
    ) -> None:
        self.message_bus = message_bus
        self.channels = channels
        self.events = events
        self.start_error = start_error
        self.background_started = asyncio.Event()
        self._finish = asyncio.Event()
        self._error: Exception | None = None
        self._dispatcher_task: asyncio.Task[None] | None = None
        self.stop_calls = 0

    @property
    def dispatcher_task(self) -> asyncio.Task[None] | None:
        return self._dispatcher_task

    async def start_all(self) -> None:
        self.events.append("channel_manager.start")
        if self.start_error is not None:
            await asyncio.sleep(0)
            raise self.start_error
        self._dispatcher_task = asyncio.create_task(self._run_dispatcher())
        await self.background_started.wait()

    async def stop_all(self) -> None:
        self.events.append("channel_manager.stop")
        self.stop_calls += 1
        task = self._dispatcher_task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def fail(self, error: Exception) -> None:
        self._error = error
        self._finish.set()

    async def _run_dispatcher(self) -> None:
        self.events.append("channel_manager.task.start")
        self.background_started.set()
        await self._finish.wait()
        if self._error is not None:
            raise self._error


class RecordingCronService(CronService):
    def __init__(
        self,
        callback: CronCallback,
        workspace: Path,
        events: list[str],
        *,
        record_events: bool = True,
    ) -> None:
        self.callback = callback
        self.workspace = workspace
        self.events = events
        self._record_events = record_events
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        if self._record_events:
            self.events.append("cron.start")
        self.started = True

    async def stop(self) -> None:
        if self._record_events:
            self.events.append("cron.stop")
        self.stopped = True


class ApplicationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._workspace = Path(self._temporary_directory.name) / "workspace"

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _config(self) -> NanobotConfig:
        return _config(self._workspace)

    async def test_manages_cron_service_lifecycle(self) -> None:
        events: list[str] = []
        loop = RecordingLoop(events)
        cron_services: list[RecordingCronService] = []

        def cron_service_factory(callback: CronCallback, workspace: Path) -> CronService:
            service = RecordingCronService(callback, workspace, events)
            cron_services.append(service)
            return service

        app, manager = _fake_application(
            events,
            loop,
            workspace=self._workspace,
            cron_service_factory=cron_service_factory,
        )
        cron_service = cron_services[0]

        await app.start()

        self.assertIs(app.cron_service, cron_service)
        self.assertTrue(cron_service.started)
        self.assertLess(events.index("channel_manager.start"), events.index("cron.start"))
        await app.close()

        self.assertTrue(cron_service.stopped)
        self.assertLess(events.index("cron.stop"), events.index("channel_manager.stop"))
        self.assertEqual(manager.stop_calls, 1)

    async def test_assembles_shared_dependencies_and_closes_in_reverse_order(self) -> None:
        events: list[str] = []
        received_context_builders: list[ContextBuilder] = []
        received_memory_consolidators: list[MemoryConsolidator] = []
        channel: RecordingChannel | None = None
        loop = RecordingLoop(events)

        def channel_factory(
            name: str,
            message_bus: MessageBus,
            config: NanobotConfig,
        ) -> BaseChannel:
            del config
            nonlocal channel
            channel = RecordingChannel(name, message_bus, events)
            return channel

        def mcp_factory(
            registry: ToolRegistry,
            servers: Mapping[str, MCPServerConfig],
        ) -> FakeMCPProvider:
            return FakeMCPProvider(registry, servers, events)

        def agent_loop_factory(
            runner: AgentRunner,
            provider: LLMProvider,
            registry: ToolRegistry,
            session_manager: Any,
            context_builder: ContextBuilder,
            session_compactor: Any,
            memory_store: Any,
            memory_consolidator: MemoryConsolidator,
            bus: MessageBus,
            subagent_manager: Any,
        ) -> RecordingLoop:
            del runner, provider, registry, session_manager, session_compactor, memory_store, subagent_manager
            received_context_builders.append(context_builder)
            received_memory_consolidators.append(memory_consolidator)
            return _configure_loop(loop, bus)

        app = Application(
            self._config(),
            provider_factory=lambda config: FakeProvider(),
            channel_factory=channel_factory,
            mcp_provider_factory=mcp_factory,
            tool_loader=NoopToolLoader(),
            agent_loop_factory=agent_loop_factory,
        )

        await app.start()
        await asyncio.wait_for(loop.started.wait(), timeout=1)

        self.assertIsNotNone(channel)
        self.assertIs(channel.message_bus if channel is not None else None, app.message_bus)
        self.assertIs(loop.message_bus, app.message_bus)
        self.assertIs(app.mcp_provider.registry, app.tool_registry)
        self.assertEqual(len(received_context_builders), 1)
        self.assertEqual(len(received_memory_consolidators), 1)
        self.assertIsInstance(received_memory_consolidators[0], MemoryConsolidator)
        self.assertTrue(app.tool_registry.has("get_weather"))
        self.assertTrue(app.channel_manager.dispatcher_running)
        self.assertIsNotNone(app.agent_task)

        agent_task = app.agent_task
        await app.close()
        await app.close()

        self.assertEqual(events[0], "mcp.connect")
        self.assertCountEqual(events[1:3], ["channel.start", "loop.start"])
        self.assertEqual(
            events[-4:],
            ["channel.close", "loop.close", "loop.background.close", "mcp.close"],
        )
        self.assertFalse(app.channel_manager.dispatcher_running)
        self.assertIsNone(app.agent_task)
        self.assertTrue(agent_task.cancelled() if agent_task is not None else False)

    async def test_injects_application_subagent_manager_into_tool_context(self) -> None:
        events: list[str] = []
        loop = RecordingLoop(events)
        tool_loader = NoopToolLoader()
        app = Application(
            self._config(),
            provider_factory=lambda config: FakeProvider(),
            channel_factory=lambda name, bus, config: RecordingChannel(name, bus, events),
            mcp_provider_factory=lambda registry, servers: FakeMCPProvider(
                registry,
                servers,
                events,
            ),
            tool_loader=tool_loader,
            agent_loop_factory=lambda runner, provider, registry, session_manager, context_builder, session_compactor, memory_store, memory_consolidator, bus, subagent_manager: _configure_loop(loop, bus),
        )

        self.assertEqual(len(tool_loader.contexts), 2)
        self.assertIsNone(tool_loader.contexts[0].subagent_manager)
        self.assertIs(tool_loader.contexts[1].subagent_manager, app.subagent_manager)

    async def test_start_failure_closes_started_resources(self) -> None:
        events: list[str] = []
        loop = RecordingLoop(events)

        def channel_factory(
            name: str,
            message_bus: MessageBus,
            config: NanobotConfig,
        ) -> BaseChannel:
            del config
            return FailingChannel(name, message_bus, events)

        app = Application(
            self._config(),
            provider_factory=lambda config: FakeProvider(),
            channel_factory=channel_factory,
            mcp_provider_factory=lambda registry, servers: FakeMCPProvider(
                registry,
                servers,
                events,
            ),
            tool_loader=NoopToolLoader(),
            agent_loop_factory=lambda runner, provider, registry, session_manager, context_builder, session_compactor, memory_store, memory_consolidator, bus, subagent_manager: loop,
        )

        with self.assertRaisesRegex(RuntimeError, "channel unavailable"):
            await app.start()

        self.assertEqual(events, [
            "mcp.connect",
            "channel.start",
            "loop.start",
            "channel.close",
            "loop.close",
            "loop.background.close",
            "mcp.close",
        ])
        self.assertIsNone(app.agent_task)

    async def test_run_waits_for_a_stop_request(self) -> None:
        events: list[str] = []
        loop = RecordingLoop(events)
        app = Application(
            self._config(),
            provider_factory=lambda config: FakeProvider(),
            channel_factory=lambda name, bus, config: RecordingChannel(name, bus, events),
            mcp_provider_factory=lambda registry, servers: FakeMCPProvider(
                registry,
                servers,
                events,
            ),
            tool_loader=NoopToolLoader(),
            agent_loop_factory=lambda runner, provider, registry, session_manager, context_builder, session_compactor, memory_store, memory_consolidator, bus, subagent_manager: loop,
        )

        task = asyncio.create_task(app.run())
        await asyncio.wait_for(loop.started.wait(), timeout=1)
        app.request_stop()
        await asyncio.wait_for(task, timeout=1)

        self.assertEqual(
            events[-4:],
            ["channel.close", "loop.close", "loop.background.close", "mcp.close"],
        )

    async def test_start_returns_while_the_background_tasks_keep_running(self) -> None:
        events: list[str] = []
        loop = RecordingLoop(events)
        app, manager = _fake_application(events, loop, workspace=self._workspace)

        await asyncio.wait_for(app.start(), timeout=0.1)
        await asyncio.wait_for(loop.started.wait(), timeout=0.1)

        self.assertTrue(manager.background_started.is_set())
        self.assertFalse(app.agent_task.done() if app.agent_task is not None else True)
        self.assertFalse(app.channel_task.done() if app.channel_task is not None else True)

        await app.close()

    async def test_agent_loop_failure_stops_the_application(self) -> None:
        events: list[str] = []
        loop = RecordingLoop(events)
        app, manager = _fake_application(events, loop, workspace=self._workspace)

        run_task = asyncio.create_task(app.run())
        await asyncio.wait_for(loop.started.wait(), timeout=1)
        await asyncio.wait_for(manager.background_started.wait(), timeout=1)
        loop.fail(RuntimeError("agent loop failed"))

        with self.assertRaisesRegex(RuntimeError, "agent loop failed"):
            await asyncio.wait_for(run_task, timeout=1)

        self.assertEqual(
            events[-4:],
            ["loop.close", "channel_manager.stop", "loop.background.close", "mcp.close"],
        )
        self.assertEqual(manager.stop_calls, 1)

    async def test_channel_manager_failure_stops_the_application(self) -> None:
        events: list[str] = []
        loop = RecordingLoop(events)
        app, manager = _fake_application(events, loop, workspace=self._workspace)

        run_task = asyncio.create_task(app.run())
        await asyncio.wait_for(loop.started.wait(), timeout=1)
        await asyncio.wait_for(manager.background_started.wait(), timeout=1)
        manager.fail(RuntimeError("dispatcher failed"))

        with self.assertRaisesRegex(RuntimeError, "dispatcher failed"):
            await asyncio.wait_for(run_task, timeout=1)

        self.assertEqual(
            events[-4:],
            ["channel_manager.stop", "loop.close", "loop.background.close", "mcp.close"],
        )

    async def test_unexpected_agent_loop_completion_stops_the_application(self) -> None:
        events: list[str] = []
        loop = RecordingLoop(events)
        app, manager = _fake_application(events, loop, workspace=self._workspace)

        run_task = asyncio.create_task(app.run())
        await asyncio.wait_for(loop.started.wait(), timeout=1)
        await asyncio.wait_for(manager.background_started.wait(), timeout=1)
        loop.finish()

        with self.assertRaisesRegex(RuntimeError, "AgentLoop stopped unexpectedly"):
            await asyncio.wait_for(run_task, timeout=1)

        self.assertEqual(
            events[-4:],
            ["loop.close", "channel_manager.stop", "loop.background.close", "mcp.close"],
        )

    async def test_start_failure_releases_started_agent_and_mcp_resources(self) -> None:
        events: list[str] = []
        loop = RecordingLoop(events)
        app, manager = _fake_application(
            events,
            loop,
            workspace=self._workspace,
            manager_start_error=RuntimeError("channel manager unavailable"),
        )

        with self.assertRaisesRegex(RuntimeError, "channel manager unavailable"):
            await app.start()

        self.assertEqual(
            events[-4:],
            ["channel_manager.stop", "loop.close", "loop.background.close", "mcp.close"],
        )
        self.assertEqual(manager.stop_calls, 1)
        self.assertIsNone(app.agent_task)

    async def test_cancelling_run_closes_all_resources(self) -> None:
        events: list[str] = []
        loop = RecordingLoop(events)
        app, manager = _fake_application(events, loop, workspace=self._workspace)

        run_task = asyncio.create_task(app.run())
        await asyncio.wait_for(loop.started.wait(), timeout=1)
        await asyncio.wait_for(manager.background_started.wait(), timeout=1)
        run_task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(run_task, timeout=1)

        self.assertEqual(
            events[-4:],
            ["channel_manager.stop", "loop.close", "loop.background.close", "mcp.close"],
        )
        self.assertEqual(manager.stop_calls, 1)


def _config(workspace: Path) -> NanobotConfig:
    return NanobotConfig(
        provider=ProviderConfig(
            type="openai_compat",
            api_key="test-key",
            api_base="https://example.test/v1",
            default_model="test-model",
        ),
        workspace=workspace,
        default_channel="fake",
        mcp_servers={"fake": MCPServerConfig(command="python")},
    )


def _configure_loop(loop: RecordingLoop, message_bus: MessageBus) -> RecordingLoop:
    loop.message_bus = message_bus
    return loop


def _fake_application(
    events: list[str],
    loop: RecordingLoop,
    *,
    workspace: Path,
    manager_start_error: Exception | None = None,
    cron_service_factory: Callable[[CronCallback, Path], CronService] | None = None,
) -> tuple[Application, FakeChannelManager]:
    managers: list[FakeChannelManager] = []
    cron_service_factory = cron_service_factory or (
        lambda callback, workspace: RecordingCronService(
            callback,
            workspace,
            events,
            record_events=False,
        )
    )

    def manager_factory(
        message_bus: MessageBus,
        channels: tuple[BaseChannel, ...],
    ) -> FakeChannelManager:
        manager = FakeChannelManager(
            message_bus,
            channels,
            events,
            start_error=manager_start_error,
        )
        managers.append(manager)
        return manager

    app = Application(
        _config(workspace),
        provider_factory=lambda config: FakeProvider(),
        channel_factory=lambda name, bus, config: RecordingChannel(name, bus, events),
        mcp_provider_factory=lambda registry, servers: FakeMCPProvider(
            registry,
            servers,
            events,
        ),
        tool_loader=NoopToolLoader(),
        agent_loop_factory=lambda runner, provider, registry, session_manager, context_builder, session_compactor, memory_store, memory_consolidator, bus, subagent_manager: _configure_loop(loop, bus),
        channel_manager_factory=manager_factory,
        cron_service_factory=cron_service_factory,
    )
    return app, managers[0]
