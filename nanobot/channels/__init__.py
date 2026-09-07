"""External-channel abstractions backed by the internal message bus."""

from .base import BaseChannel
from .factory import ChannelFactory, create_default_channel_factory
from .fake import FakeChannel
from .manager import ChannelManager
from .qq import QQChannel
from .websocket import WebSocketChannel

__all__ = [
    "BaseChannel",
    "ChannelFactory",
    "ChannelManager",
    "FakeChannel",
    "QQChannel",
    "WebSocketChannel",
    "create_default_channel_factory",
]
