"""Focused tests for routing due Cron tasks through the MessageBus."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta, timezone

from nanobot.agent import AgentLoop, AgentRunner, ContextBuilder
from nanobot.bus import InboundMessage, MessageBus
from nanobot.cron import (
    CronMessagePublisher,
    CronPayload,
    CronSchedule,
    CronService,
    CronTask,
    cron_task_to_inbound_message,
)
from nanobot.providers import BaseMessage, LLMProvider, LLMResponse
from nanobot.session import SessionManager
from nanobot.tools import Tool, ToolRegistry


class FailingMessageBus(MessageBus):
    async def publish_inbound(self, message: InboundMessage) -> None:
        del message
        raise RuntimeError("expected publish failure")


class ScriptedProvider(LLMProvider):
    def __init__(self, response: LLMResponse) -> None:
        self._response = response
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
        return self._response

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        raise AssertionError("Cron publisher tests do not use streaming")


class CronMessagePublisherTest(unittest.IsolatedAsyncioTestCase):
    async def test_converts_routing_fields_to_a_normal_inbound_message(self) -> None:
        inbound = cron_task_to_inbound_message(_task())

        self.assertEqual(inbound.channel, "fake")
        self.assertEqual(inbound.chat_id, "chat-1")
        self.assertEqual(inbound.sender_id, "cron")
        self.assertEqual(inbound.session_id, "session-1")
        self.assertEqual(inbound.content, _scheduled_message("Send the scheduled reminder."))
        self.assertEqual(
            inbound.metadata,
            {
                "qq_chat_type": "c2c",
                "source": "cron",
                "cron_task_id": "reminder",
            },
        )

    async def test_publishes_the_converted_message_to_the_bus(self) -> None:
        bus = MessageBus()
        publisher = CronMessagePublisher(bus)

        await publisher.publish(_task())

        self.assertEqual(await bus.consume_inbound(), cron_task_to_inbound_message(_task()))

    async def test_publish_failure_does_not_stop_the_cron_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            publisher = CronMessagePublisher(FailingMessageBus())
            service = CronService(publisher.publish, temporary_directory)
            service.add_at(
                datetime.now(timezone.utc) + timedelta(milliseconds=20),
                task_id="failing-publish",
                channel="fake",
                chat_id="chat-1",
                sender_id="cron",
                session_key="session-1",
                message="This publish fails.",
            )

            await service.start()
            try:
                await asyncio.sleep(0.06)
                self.assertTrue(service.is_running)
                task = service.get("failing-publish")
                self.assertFalse(task.enabled)
                self.assertEqual(task.state.last_error, "RuntimeError")
            finally:
                await service.stop()

    async def test_due_task_flows_through_bus_agent_loop_and_outbound_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            bus = MessageBus()
            provider = ScriptedProvider(LLMResponse(content="Scheduled answer."))
            session_manager = SessionManager(temporary_directory)
            loop = AgentLoop(
                AgentRunner(),
                provider,
                ToolRegistry(),
                session_manager,
                ContextBuilder(temporary_directory),
                message_bus=bus,
            )
            publisher = CronMessagePublisher(bus)
            service = CronService(publisher.publish, temporary_directory)
            service.add_at(
                datetime.now(timezone.utc) + timedelta(milliseconds=20),
                task_id="agent-task",
                channel="fake",
                chat_id="chat-1",
                sender_id="cron",
                session_key="session-1",
                message="Run the scheduled task.",
            )
            worker = asyncio.create_task(loop.run())
            await service.start()
            try:
                outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
                saved_messages = session_manager.get_or_create("session-1").messages
            finally:
                await service.stop()
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await worker

        self.assertEqual(outbound.channel, "fake")
        self.assertEqual(outbound.chat_id, "chat-1")
        self.assertEqual(outbound.session_id, "session-1")
        self.assertEqual(outbound.content, "Scheduled answer.")
        self.assertEqual(outbound.metadata["source"], "cron")
        self.assertEqual(
            provider.complete_calls[0][-1].content,
            _scheduled_message("Run the scheduled task."),
        )
        self.assertEqual(saved_messages[-2].content, _scheduled_message("Run the scheduled task."))
        self.assertEqual(saved_messages[-1].content, "Scheduled answer.")


def _task() -> CronTask:
    return CronTask(
        id="reminder",
        schedule=CronSchedule(
            kind="at",
            at_ms=round(datetime.now(timezone.utc).timestamp() * 1000),
        ),
        payload=CronPayload(
            channel="fake",
            chat_id="chat-1",
            sender_id="cron",
            session_key="session-1",
            message="Send the scheduled reminder.",
            metadata={"qq_chat_type": "c2c"},
        ),
    )


def _scheduled_message(message: str) -> str:
    return (
        "The scheduled time has arrived. Execute this scheduled cron job now and "
        "report the result to the user in the same session.\n\n"
        "Rules:\n\n"
        "- Speak directly to the user in their language.\n"
        "- Do not narrate internal progress.\n"
        "- Do not include user IDs.\n"
        "- Do not add status reports like \"Done\" or \"Reminded\" unless they are "
        "the natural response.\n\n"
        f"Cron job: {message}"
    )
