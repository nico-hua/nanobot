"""Focused tests for long-running Application assembly and shutdown."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import patch

from nanobot.agent import AgentRunner
from nanobot.bus import MessageBus
from nanobot.channels import BaseChannel, ChannelManager, FakeChannel
from nanobot.cli import Application
from nanobot.config import MCPServerConfig, NanobotConfig, ProviderConfig
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
    def load(self, registry: ToolRegistry, context: ToolContext) -> tuple[str, ...]:
        del registry, context
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

    async def run(self) -> None:
        self.events.append("loop.start")
        self.started.set()
        try:
            await asyncio.Future()
        finally:
            self.events.append("loop.close")


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


class ApplicationTest(unittest.IsolatedAsyncioTestCase):
    def test_initialization_configures_logging_from_json_config(self) -> None:
        with patch("nanobot.cli.application.configure_logging_from_config") as configure_logging:
            Application(
                _config(),
                provider_factory=lambda config: FakeProvider(),
                channel_factory=lambda name, bus, config: RecordingChannel(name, bus, []),
                mcp_provider_factory=lambda registry, servers: FakeMCPProvider(
                    registry,
                    servers,
                    [],
                ),
                tool_loader=NoopToolLoader(),
                agent_loop_factory=lambda runner, provider, registry, bus: RecordingLoop([]),
            )

        configure_logging.assert_called_once_with()

    async def test_assembles_shared_dependencies_and_closes_in_reverse_order(self) -> None:
        events: list[str] = []
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

        app = Application(
            _config(),
            provider_factory=lambda config: FakeProvider(),
            channel_factory=channel_factory,
            mcp_provider_factory=mcp_factory,
            tool_loader=NoopToolLoader(),
            agent_loop_factory=lambda runner, provider, registry, bus: _configure_loop(
                loop,
                bus,
            ),
        )

        await app.start()
        await asyncio.wait_for(loop.started.wait(), timeout=1)

        self.assertIsNotNone(channel)
        self.assertIs(channel.message_bus if channel is not None else None, app.message_bus)
        self.assertIs(loop.message_bus, app.message_bus)
        self.assertIs(app.mcp_provider.registry, app.tool_registry)
        self.assertTrue(app.tool_registry.has("get_weather"))
        self.assertTrue(app.channel_manager.dispatcher_running)
        self.assertIsNotNone(app.agent_task)

        agent_task = app.agent_task
        await app.close()
        await app.close()

        self.assertEqual(events[0], "mcp.connect")
        self.assertCountEqual(events[1:3], ["channel.start", "loop.start"])
        self.assertEqual(events[-3:], ["channel.close", "loop.close", "mcp.close"])
        self.assertFalse(app.channel_manager.dispatcher_running)
        self.assertIsNone(app.agent_task)
        self.assertTrue(agent_task.cancelled() if agent_task is not None else False)

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
            _config(),
            provider_factory=lambda config: FakeProvider(),
            channel_factory=channel_factory,
            mcp_provider_factory=lambda registry, servers: FakeMCPProvider(
                registry,
                servers,
                events,
            ),
            tool_loader=NoopToolLoader(),
            agent_loop_factory=lambda runner, provider, registry, bus: loop,
        )

        with self.assertRaisesRegex(RuntimeError, "channel unavailable"):
            await app.start()

        self.assertEqual(events, [
            "mcp.connect",
            "channel.start",
            "loop.start",
            "channel.close",
            "loop.close",
            "mcp.close",
        ])
        self.assertIsNone(app.agent_task)

    async def test_run_waits_for_a_stop_request(self) -> None:
        events: list[str] = []
        loop = RecordingLoop(events)
        app = Application(
            _config(),
            provider_factory=lambda config: FakeProvider(),
            channel_factory=lambda name, bus, config: RecordingChannel(name, bus, events),
            mcp_provider_factory=lambda registry, servers: FakeMCPProvider(
                registry,
                servers,
                events,
            ),
            tool_loader=NoopToolLoader(),
            agent_loop_factory=lambda runner, provider, registry, bus: loop,
        )

        task = asyncio.create_task(app.run())
        await asyncio.wait_for(loop.started.wait(), timeout=1)
        app.request_stop()
        await asyncio.wait_for(task, timeout=1)

        self.assertEqual(events[-3:], ["channel.close", "loop.close", "mcp.close"])


def _config() -> NanobotConfig:
    return NanobotConfig(
        provider=ProviderConfig(
            type="openai_compat",
            api_key="test-key",
            api_base="https://example.test/v1",
            default_model="test-model",
        ),
        workspace=Path("workspace"),
        default_channel="fake",
        mcp_servers={"fake": MCPServerConfig(command="python")},
    )


def _configure_loop(loop: RecordingLoop, message_bus: MessageBus) -> RecordingLoop:
    loop.message_bus = message_bus
    return loop
