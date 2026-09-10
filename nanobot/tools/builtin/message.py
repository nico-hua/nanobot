"""A built-in tool for sending one proactive message to the current channel."""

from __future__ import annotations

import asyncio
import logging

from ...bus import MessageBus, OutboundMessage
from ..base import Tool, ToolParameter, ToolResult
from ..context import RequestContext, ToolContext, get_request_context

logger = logging.getLogger(__name__)


class MessageTool(Tool):
    """Publish a plain outbound message using the current request route."""

    def __init__(self, message_bus: MessageBus) -> None:
        if not isinstance(message_bus, MessageBus):
            raise TypeError("MessageTool requires a MessageBus")
        self._message_bus = message_bus
        super().__init__(
            name="message",
            description=(
                "Send a text message to the current conversation immediately. "
                "Use it only when a separate proactive message is needed."
            ),
            parameters=(
                ToolParameter(
                    name="content",
                    description="The non-empty text to send to the current conversation.",
                    type="string",
                    required=True,
                ),
            ),
        )

    @classmethod
    def enabled(cls, context: ToolContext) -> bool:
        return isinstance(context.message_bus, MessageBus)

    @classmethod
    def create(cls, context: ToolContext) -> MessageTool:
        if not isinstance(context.message_bus, MessageBus):
            raise TypeError("MessageTool requires a MessageBus")
        return cls(context.message_bus)

    async def execute(self, content: str) -> ToolResult:
        """Queue one ordinary outbound message for the current channel."""

        if not isinstance(content, str) or not content.strip():
            return _tool_error("content must be a non-empty string")

        request_context = get_request_context()
        if request_context is None:
            return _tool_error(
                "message requires an active request context with a channel and chat ID"
            )

        try:
            outbound = _outbound_message(request_context, content)
        except (TypeError, ValueError):
            # RequestContext normally prevents this state, but keep the tool's
            # failure contract explicit if a host binds an invalid context.
            return _tool_error(
                "message requires an active request context with a channel and chat ID"
            )

        try:
            await self._message_bus.publish_outbound(outbound)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "Message tool could not publish an outbound message (error_type=%s)",
                type(error).__name__,
            )
            return _tool_error("message could not be published")

        return ToolResult(content="Message sent.")


def _outbound_message(
    request_context: RequestContext,
    content: str,
) -> OutboundMessage:
    """Build a normal delivery event without inheriting stream event markers."""

    metadata = dict(request_context.metadata)
    metadata.pop("event", None)
    # QQ uses this source marker to send proactively instead of replying with
    # the cached inbound message ID.
    metadata["source"] = "message"
    return OutboundMessage(
        channel=request_context.channel,
        chat_id=request_context.chat_id,
        sender_id=request_context.sender_id,
        session_id=request_context.session_key,
        content=content,
        metadata=metadata,
    )


def _tool_error(message: str) -> ToolResult:
    return ToolResult(content=f"Error: {message}", success=False, error=message)
