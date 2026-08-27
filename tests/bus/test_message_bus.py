"""Focused tests for MessageBus and AgentLoop bus integration."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Awaitable, Callable, Sequence

from nanobot.agent import AgentLoop, AgentRunner
from nanobot.bus import InboundMessage, MessageBus, OutboundMessage
from nanobot.providers import BaseMessage, LLMProvider, LLMResponse
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
        raise AssertionError("MessageBus tests must not use streaming")


class MessageBusTest(unittest.IsolatedAsyncioTestCase):
    async def test_publishes_and_consumes_each_message_direction(self) -> None:
        bus = MessageBus()
        inbound = InboundMessage("test", "chat-1", "session-1", "Hello")
        outbound = OutboundMessage("test", "chat-1", "session-1", "Hi")

        await bus.publish_inbound(inbound)
        await bus.publish_outbound(outbound)

        self.assertEqual(await bus.consume_inbound(), inbound)
        self.assertEqual(await bus.consume_outbound(), outbound)


class AgentLoopBusTest(unittest.IsolatedAsyncioTestCase):
    async def test_consumes_an_inbound_message_and_publishes_its_response(self) -> None:
        bus = MessageBus()
        provider = ScriptedProvider((LLMResponse(content="Hello back."),))
        loop = AgentLoop(AgentRunner(), provider, ToolRegistry(), message_bus=bus)
        inbound = InboundMessage("test", "chat-1", "session-1", "Hello")

        await bus.publish_inbound(inbound)
        worker = asyncio.create_task(loop.run())
        outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        await _cancel_worker(self, worker)

        self.assertEqual(
            outbound,
            OutboundMessage("test", "chat-1", "session-1", "Hello back."),
        )
        self.assertEqual(provider.complete_calls[0][-1].content, "Hello")

    async def test_keeps_different_session_histories_separate(self) -> None:
        bus = MessageBus()
        provider = ScriptedProvider(
            (
                LLMResponse(content="Answer one."),
                LLMResponse(content="Answer two."),
            )
        )
        loop = AgentLoop(AgentRunner(), provider, ToolRegistry(), message_bus=bus)
        await bus.publish_inbound(InboundMessage("test", "chat-1", "one", "First"))
        await bus.publish_inbound(InboundMessage("test", "chat-2", "two", "Second"))

        worker = asyncio.create_task(loop.run())
        first = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        second = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        await _cancel_worker(self, worker)

        self.assertEqual((first.session_id, second.session_id), ("one", "two"))
        self.assertEqual(provider.complete_calls[0][-1].content, "First")
        self.assertEqual(provider.complete_calls[1][-1].content, "Second")
        self.assertEqual(len(provider.complete_calls[1]), 1)

    async def test_cancellation_leaves_no_agent_worker_running(self) -> None:
        bus = MessageBus()
        loop = AgentLoop(
            AgentRunner(),
            ScriptedProvider(()),
            ToolRegistry(),
            message_bus=bus,
        )
        worker = asyncio.create_task(loop.run())
        await asyncio.sleep(0)

        await _cancel_worker(self, worker)


async def _cancel_worker(
    test_case: unittest.IsolatedAsyncioTestCase,
    worker: asyncio.Task[object],
) -> None:
    worker.cancel()
    with test_case.assertRaises(asyncio.CancelledError):
        await worker
    test_case.assertTrue(worker.done())
