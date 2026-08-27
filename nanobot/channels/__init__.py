"""External-channel abstractions backed by the internal message bus."""

from .base import BaseChannel
from .fake import FakeChannel
from .manager import ChannelManager
from .qq import QQChannel

__all__ = ["BaseChannel", "ChannelManager", "FakeChannel", "QQChannel"]
