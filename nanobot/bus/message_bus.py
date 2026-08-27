"""Minimal asyncio.Queue message bus for agent input and output."""

from __future__ import annotations

import asyncio

from .messages import InboundMessage, OutboundMessage


class MessageBus:
    """Publish and consume inbound and outbound messages in memory."""

    def __init__(self) -> None:
        self._inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()

    async def publish_inbound(self, message: InboundMessage) -> None:
        """Queue one user message for agent processing."""

        if not isinstance(message, InboundMessage):
            raise TypeError("MessageBus requires an InboundMessage")
        await self._inbound.put(message)

    async def consume_inbound(self) -> InboundMessage:
        """Wait for and return the next inbound message."""

        return await self._inbound.get()

    async def publish_outbound(self, message: OutboundMessage) -> None:
        """Queue one agent response for a channel to deliver."""

        if not isinstance(message, OutboundMessage):
            raise TypeError("MessageBus requires an OutboundMessage")
        await self._outbound.put(message)

    async def consume_outbound(self) -> OutboundMessage:
        """Wait for and return the next outbound message."""

        return await self._outbound.get()
