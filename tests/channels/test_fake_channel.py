"""End-to-end tests for FakeChannel, MessageBus, and AgentLoop."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Awaitable, Callable, Sequence

from nanobot.agent import AgentLoop, AgentRunner
from nanobot.bus import MessageBus
from nanobot.channels import FakeChannel
from nanobot.providers import BaseMessage, LLMProvider, LLMResponse
from nanobot.tools import Tool, ToolRegistry


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
        raise AssertionError("FakeChannel tests must not use streaming")


class FakeChannelTest(unittest.IsolatedAsyncioTestCase):
    async def test_external_input_becomes_an_inbound_bus_message(self) -> None:
        bus = MessageBus()
        channel = FakeChannel("fake", bus)

        message = await channel.receive_external(
            "Hello",
            "chat-1",
            "sender-1",
            "session-1",
        )

        self.assertEqual(await bus.consume_inbound(), message)
        self.assertEqual(message.channel, "fake")
        self.assertEqual(message.sender_id, "sender-1")

    async def test_end_to_end_flow_saves_fake_channel_output(self) -> None:
        bus = MessageBus()
        channel = FakeChannel("fake", bus)
        provider = ScriptedProvider((LLMResponse(content="Hello back."),))
        loop = AgentLoop(AgentRunner(), provider, ToolRegistry(), message_bus=bus)
        await channel.start()
        await channel.receive_external(
            "Hello",
            "chat-1",
            "sender-1",
            "session-1",
        )

        worker = asyncio.create_task(loop.run())
        outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        await channel.send(outbound)
        await _cancel_worker(self, worker)

        self.assertEqual(channel.sent_messages, [outbound])
        self.assertEqual(outbound.channel, "fake")
        self.assertEqual(outbound.chat_id, "chat-1")
        self.assertEqual(outbound.sender_id, "sender-1")
        self.assertEqual(outbound.session_id, "session-1")

    async def test_preserves_routes_for_different_channels_and_chats(self) -> None:
        bus = MessageBus()
        alpha = FakeChannel("alpha", bus)
        beta = FakeChannel("beta", bus)
        provider = ScriptedProvider(
            (LLMResponse(content="Alpha answer."), LLMResponse(content="Beta answer."))
        )
        loop = AgentLoop(AgentRunner(), provider, ToolRegistry(), message_bus=bus)
        await alpha.receive_external("One", "chat-a", "sender-a", "session-a")
        await beta.receive_external("Two", "chat-b", "sender-b", "session-b")

        worker = asyncio.create_task(loop.run())
        for _ in range(2):
            outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
            await {"alpha": alpha, "beta": beta}[outbound.channel].send(outbound)
        await _cancel_worker(self, worker)

        self.assertEqual(alpha.sent_messages[0].chat_id, "chat-a")
        self.assertEqual(alpha.sent_messages[0].sender_id, "sender-a")
        self.assertEqual(beta.sent_messages[0].chat_id, "chat-b")
        self.assertEqual(beta.sent_messages[0].sender_id, "sender-b")

    async def test_channel_start_and_stop_update_its_lifecycle_state(self) -> None:
        channel = FakeChannel("fake", MessageBus())

        self.assertFalse(channel.started)
        await channel.start()
        self.assertTrue(channel.started)
        await channel.stop()
        self.assertFalse(channel.started)


async def _cancel_worker(
    test_case: unittest.IsolatedAsyncioTestCase,
    worker: asyncio.Task[object],
) -> None:
    worker.cancel()
    with test_case.assertRaises(asyncio.CancelledError):
        await worker
