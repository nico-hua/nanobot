"""Factories for creating configured external channels."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from ..bus import MessageBus
from ..config import NanobotConfig
from .base import BaseChannel
from .qq import QQChannel
from .websocket import WebSocketChannel

ChannelConstructor = Callable[[str, MessageBus, NanobotConfig], BaseChannel]


class ChannelFactory:
    """Create external channels from registered channel type constructors."""

    def __init__(self, constructors: Mapping[str, ChannelConstructor] | None = None) -> None:
        self._constructors = dict(constructors or {})

    def register(self, channel_type: str, constructor: ChannelConstructor) -> None:
        """Register one constructor without silently replacing an existing type."""

        if not isinstance(channel_type, str) or not channel_type.strip():
            raise ValueError("Channel type must be a non-empty string")
        if channel_type in self._constructors:
            raise ValueError(f"Channel type is already registered: {channel_type}")
        self._constructors[channel_type] = constructor

    def create(
        self,
        channel_name: str,
        message_bus: MessageBus,
        config: NanobotConfig,
    ) -> BaseChannel:
        """Create the channel selected by the configured default channel name."""

        constructor = self._constructors.get(channel_name)
        if constructor is None:
            raise ValueError(f"Unsupported configured channel: {channel_name}")
        return constructor(channel_name, message_bus, config)


def create_default_channel_factory() -> ChannelFactory:
    """Return a factory with the channels supported by this project."""

    return ChannelFactory(
        {
            "qq": _create_qq_channel,
            "websocket": _create_websocket_channel,
        }
    )


def _create_qq_channel(
    channel_name: str,
    message_bus: MessageBus,
    config: NanobotConfig,
) -> BaseChannel:
    if config.qq is None:
        raise ValueError("The qq channel requires QQ credentials in nanobot.json")
    return QQChannel(channel_name, message_bus, config.qq)


def _create_websocket_channel(
    channel_name: str,
    message_bus: MessageBus,
    config: NanobotConfig,
) -> BaseChannel:
    if config.websocket is None:
        raise ValueError("The websocket channel requires WebSocket settings in nanobot.json")
    return WebSocketChannel(channel_name, message_bus, config.websocket, config.auth)
