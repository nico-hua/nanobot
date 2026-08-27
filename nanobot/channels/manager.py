"""Lifecycle management and outbound routing for registered channels."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from ..bus import MessageBus
from .base import BaseChannel

logger = logging.getLogger(__name__)


class ChannelManager:
    """Start channels and route outbound bus messages to their targets."""

    def __init__(
        self,
        message_bus: MessageBus,
        channels: Sequence[BaseChannel] = (),
    ) -> None:
        if not isinstance(message_bus, MessageBus):
            raise TypeError("ChannelManager requires a MessageBus")

        self._message_bus = message_bus
        self._channels: dict[str, BaseChannel] = {}
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._dispatch_errors: list[str] = []
        for channel in channels:
            self.register(channel)

    @property
    def channels(self) -> tuple[BaseChannel, ...]:
        """Return registered channels in registration order."""

        return tuple(self._channels.values())

    @property
    def dispatcher_running(self) -> bool:
        """Return whether the outbound dispatcher task is running."""

        return self._dispatcher_task is not None and not self._dispatcher_task.done()

    @property
    def dispatch_errors(self) -> tuple[str, ...]:
        """Return unknown-channel and delivery errors in occurrence order."""

        return tuple(self._dispatch_errors)

    def register(self, channel: BaseChannel) -> None:
        """Register one channel, rejecting duplicate names explicitly."""

        if not isinstance(channel, BaseChannel):
            raise TypeError("ChannelManager can only register BaseChannel instances")
        if channel.message_bus is not self._message_bus:
            raise ValueError("Channel must use the ChannelManager MessageBus")
        if channel.name in self._channels:
            raise ValueError(f"Channel is already registered: {channel.name}")
        self._channels[channel.name] = channel

    def get(self, name: str) -> BaseChannel | None:
        """Return the registered channel with this name, if present."""

        return self._channels.get(name)

    async def start_all(self) -> None:
        """Start all channels and the one outbound dispatcher task."""

        logger.info("Starting channels (count=%d)", len(self._channels))
        for channel in self._channels.values():
            await channel.start()
        if not self.dispatcher_running:
            self._dispatcher_task = asyncio.create_task(self._dispatch_outbound())
        logger.info("Channel manager started")

    async def stop_all(self) -> None:
        """Cancel and await dispatching before stopping all channels."""

        logger.info("Stopping channel manager")
        task = self._dispatcher_task
        self._dispatcher_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        for channel in self._channels.values():
            await channel.stop()
        logger.info("Channel manager stopped")

    async def _dispatch_outbound(self) -> None:
        while True:
            try:
                message = await asyncio.wait_for(
                    self._message_bus.consume_outbound(),
                    timeout=1,
                )
            except TimeoutError:
                continue
            channel = self.get(message.channel)
            if channel is None:
                self._dispatch_errors.append(f"Unknown channel: {message.channel}")
                logger.warning("Outbound message discarded for an unknown channel")
                continue

            try:
                await channel.send(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to send outbound message through a channel")
                self._dispatch_errors.append(
                    f"Failed to send via channel {message.channel}: {exc}"
                )
