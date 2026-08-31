"""Focused tests for ChannelManager outbound routing."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Awaitable, Callable, Sequence

from nanobot.agent import AgentLoop, AgentRunner, ContextBuilder
from nanobot.bus import MessageBus, OutboundMessage
from nanobot.channels import ChannelManager, FakeChannel
from nanobot.providers import BaseMessage, LLMProvider, LLMResponse
from nanobot.session import SessionManager
from nanobot.tools import Tool, ToolRegistry


class NotifyingFakeChannel(FakeChannel):
    def __init__(self, name: str, message_bus: MessageBus) -> None:
        super().__init__(name, message_bus)
        self.sent_event = asyncio.Event()

    async def send(self, message: OutboundMessage) -> None:
        await super().send(message)
        self.sent_event.set()


class ScriptedProvider(LLMProvider):
    def __init__(self, responses: Sequence[LLMResponse]) -> None:
        self._responses = iter(responses)

    async def complete(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        del messages, tools, max_tokens, temperature
        return next(self._responses)

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        raise AssertionError("ChannelManager tests must not use streaming")


class ChannelManagerTest(unittest.IsolatedAsyncioTestCase):
    async def test_registers_multiple_channels_and_manages_lifecycle(self) -> None:
        bus = MessageBus()
        alpha = FakeChannel("alpha", bus)
        beta = FakeChannel("beta", bus)
        manager = ChannelManager(bus, (alpha, beta))

        self.assertEqual(manager.channels, (alpha, beta))
        self.assertIs(manager.get("alpha"), alpha)
        await manager.start_all()
        self.assertTrue(alpha.started)
        self.assertTrue(beta.started)
        self.assertTrue(manager.dispatcher_running)

        await manager.stop_all()

        self.assertFalse(alpha.started)
        self.assertFalse(beta.started)
        self.assertFalse(manager.dispatcher_running)

    async def test_routes_outbound_messages_to_the_matching_channel(self) -> None:
        bus = MessageBus()
        channel = NotifyingFakeChannel("fake", bus)
        manager = ChannelManager(bus, (channel,))
        message = OutboundMessage(
            "fake",
            "chat-1",
            "sender-1",
            "session-1",
            "Hello.",
        )

        await manager.start_all()
        try:
            await bus.publish_outbound(message)
            await asyncio.wait_for(channel.sent_event.wait(), timeout=1)
        finally:
            await manager.stop_all()

        self.assertEqual(channel.sent_messages, [message])

    async def test_unknown_channel_is_recorded_without_stopping_dispatch(self) -> None:
        bus = MessageBus()
        channel = NotifyingFakeChannel("fake", bus)
        manager = ChannelManager(bus, (channel,))

        await manager.start_all()
        try:
            await bus.publish_outbound(
                OutboundMessage(
                    "missing",
                    "chat-1",
                    "sender-1",
                    "session-1",
                    "Lost message.",
                )
            )
            await _wait_until(lambda: bool(manager.dispatch_errors))
            await bus.publish_outbound(
                OutboundMessage(
                    "fake",
                    "chat-1",
                    "sender-1",
                    "session-1",
                    "Delivered message.",
                )
            )
            await asyncio.wait_for(channel.sent_event.wait(), timeout=1)
        finally:
            await manager.stop_all()

        self.assertEqual(manager.dispatch_errors, ("Unknown channel: missing",))
        self.assertEqual(channel.sent_messages[0].content, "Delivered message.")

    async def test_agent_loop_response_reaches_the_fake_channel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            bus = MessageBus()
            channel = NotifyingFakeChannel("fake", bus)
            manager = ChannelManager(bus, (channel,))
            loop = AgentLoop(
                AgentRunner(),
                ScriptedProvider((LLMResponse(content="Agent answer."),)),
                ToolRegistry(),
                SessionManager(temporary_directory),
                ContextBuilder(temporary_directory),
                message_bus=bus,
            )

            await manager.start_all()
            worker = asyncio.create_task(loop.run())
            try:
                await channel.receive_external(
                    "Question",
                    "chat-1",
                    "sender-1",
                    "session-1",
                )
                await asyncio.wait_for(channel.sent_event.wait(), timeout=1)
            finally:
                await _cancel_worker(self, worker)
                await manager.stop_all()

            self.assertEqual(channel.sent_messages[0].content, "Agent answer.")
            self.assertEqual(channel.sent_messages[0].session_id, "session-1")


async def _wait_until(predicate: Callable[[], bool]) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=1)


async def _cancel_worker(
    test_case: unittest.IsolatedAsyncioTestCase,
    worker: asyncio.Task[object],
) -> None:
    worker.cancel()
    with test_case.assertRaises(asyncio.CancelledError):
        await worker
