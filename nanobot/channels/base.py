"""Base abstraction for external message channels."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from ..bus import InboundMessage, MessageBus, OutboundMessage

logger = logging.getLogger(__name__)


class BaseChannel(ABC):
    """Adapt one external channel to the internal message bus."""

    def __init__(self, name: str, message_bus: MessageBus) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Channel name must be a non-empty string")
        if not isinstance(message_bus, MessageBus):
            raise TypeError("BaseChannel requires a MessageBus")

        self.name = name
        self._message_bus = message_bus
        self._started = False

    @property
    def started(self) -> bool:
        """Return whether the channel has been started."""

        return self._started

    @property
    def message_bus(self) -> MessageBus:
        """Return the bus used for this channel's inbound messages."""

        return self._message_bus

    async def start(self) -> None:
        """Mark the channel as ready to receive external input."""

        self._started = True
        logger.info("Channel started")

    async def stop(self) -> None:
        """Mark the channel as stopped."""

        self._started = False
        logger.info("Channel stopped")

    async def receive_external(
        self,
        content: str,
        chat_id: str,
        sender_id: str,
        session_id: str,
    ) -> InboundMessage:
        """Convert external input to an inbound message and publish it."""

        message = InboundMessage(
            channel=self.name,
            chat_id=chat_id,
            sender_id=sender_id,
            session_id=session_id,
            content=content,
        )
        await self._message_bus.publish_inbound(message)
        return message

    @abstractmethod
    async def send(self, message: OutboundMessage) -> None:
        """Deliver one outbound message through the external channel."""

        raise NotImplementedError

    def _validate_outbound_message(self, message: OutboundMessage) -> None:
        if not isinstance(message, OutboundMessage):
            raise TypeError("Channel send requires an OutboundMessage")
        if message.channel != self.name:
            raise ValueError(
                f"Outbound message for {message.channel} cannot use channel {self.name}"
            )
