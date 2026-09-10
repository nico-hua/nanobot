"""Tests for the route-aware proactive message tool."""

from __future__ import annotations

import unittest

from nanobot.bus import MessageBus, OutboundMessage
from nanobot.tools import (
    RequestContext,
    ToolContext,
    ToolLoader,
    ToolRegistry,
    bind_request_context,
)
from nanobot.tools.builtin import MessageTool


class MessageToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._message_bus = RecordingMessageBus()
        self._tool = MessageTool.create(ToolContext(message_bus=self._message_bus))

    def test_has_expected_schema_and_requires_message_bus(self) -> None:
        self.assertEqual(self._tool.name, "message")
        self.assertEqual(
            self._tool.parameters_schema,
            {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The non-empty text to send to the current conversation.",
                    }
                },
                "required": ["content"],
            },
        )
        self.assertTrue(MessageTool.enabled(ToolContext(message_bus=self._message_bus)))
        self.assertFalse(MessageTool.enabled(ToolContext()))
        with self.assertRaisesRegex(TypeError, "MessageBus"):
            MessageTool.create(ToolContext())

    async def test_publishes_current_request_route_as_a_proactive_message(self) -> None:
        request_context = _request_context(
            metadata={
                "qq_chat_type": "c2c",
                "message_id": "origin-message-1",
                "event": "delta",
            }
        )

        with bind_request_context(request_context):
            result = await self._tool.execute("Status update")

        outbound = await self._message_bus.consume_outbound()
        self.assertTrue(result.success)
        self.assertEqual(result.content, "Message sent.")
        self.assertEqual(outbound.content, "Status update")
        self.assertEqual(outbound.channel, "qq")
        self.assertEqual(outbound.chat_id, "chat-1")
        self.assertEqual(outbound.sender_id, "sender-1")
        self.assertEqual(outbound.session_id, "qq:chat-1")
        self.assertEqual(
            outbound.metadata,
            {
                "qq_chat_type": "c2c",
                "message_id": "origin-message-1",
                "source": "message",
            },
        )

    async def test_requires_an_active_request_context_with_a_delivery_target(self) -> None:
        result = await self._tool.execute("Status update")

        self.assertFalse(result.success)
        self.assertEqual(
            result.error,
            "message requires an active request context with a channel and chat ID",
        )
        self.assertEqual(self._message_bus.published, [])

    async def test_rejects_empty_content_without_publishing(self) -> None:
        with bind_request_context(_request_context()):
            result = await self._tool.execute("   ")

        self.assertFalse(result.success)
        self.assertEqual(result.error, "content must be a non-empty string")
        self.assertEqual(self._message_bus.published, [])

    async def test_returns_a_tool_error_when_bus_publication_fails(self) -> None:
        tool = MessageTool.create(ToolContext(message_bus=FailingMessageBus()))

        with bind_request_context(_request_context()):
            result = await tool.execute("Status update")

        self.assertFalse(result.success)
        self.assertEqual(result.error, "message could not be published")

    async def test_loader_and_registry_discover_and_execute_the_tool(self) -> None:
        registry = ToolRegistry()
        names = ToolLoader().load(registry, ToolContext(message_bus=self._message_bus))

        self.assertIn("message", names)
        self.assertIsInstance(registry.get("message"), MessageTool)
        with bind_request_context(_request_context()):
            result = await registry.execute("message", {"content": "Queued update"})

        outbound = await self._message_bus.consume_outbound()
        self.assertTrue(result.success)
        self.assertEqual(outbound.content, "Queued update")
        # This test has no concrete Channel at all: delivery is solely through
        # MessageBus and can be dispatched by ChannelManager later.
        self.assertEqual(outbound.metadata["source"], "message")


class FailingMessageBus(MessageBus):
    async def publish_outbound(self, message: OutboundMessage) -> None:
        del message
        raise RuntimeError("outbound queue unavailable")


class RecordingMessageBus(MessageBus):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[OutboundMessage] = []

    async def publish_outbound(self, message: OutboundMessage) -> None:
        self.published.append(message)
        await super().publish_outbound(message)


def _request_context(*, metadata: dict[str, object] | None = None) -> RequestContext:
    return RequestContext(
        session_key="qq:chat-1",
        channel="qq",
        chat_id="chat-1",
        sender_id="sender-1",
        metadata=metadata or {},
    )
