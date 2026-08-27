"""A test channel that records outbound messages in memory."""

from __future__ import annotations

from ..bus import MessageBus, OutboundMessage
from .base import BaseChannel


class FakeChannel(BaseChannel):
    """Channel implementation that keeps sent messages for assertions."""

    def __init__(self, name: str, message_bus: MessageBus) -> None:
        super().__init__(name, message_bus)
        self.sent_messages: list[OutboundMessage] = []

    async def send(self, message: OutboundMessage) -> None:
        """Record one outbound message instead of delivering it externally."""

        self._validate_outbound_message(message)
        self.sent_messages.append(message)
