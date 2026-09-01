"""Data model for persisted workspace-level memory events."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from ..providers import BaseMessage, SystemMessage


@dataclass(frozen=True)
class MemoryEvent:
    """One completed conversation turn waiting for memory consolidation."""

    event_id: int
    session_key: str
    created_at: datetime
    messages: tuple[BaseMessage, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, int) or isinstance(self.event_id, bool):
            raise TypeError("memory event ID must be an integer")
        if self.event_id <= 0:
            raise ValueError("memory event ID must be positive")
        if not isinstance(self.session_key, str) or not self.session_key.strip():
            raise ValueError("memory event session_key must be a non-empty string")
        if not isinstance(self.created_at, datetime):
            raise TypeError("memory event created_at must be a datetime")
        if self.created_at.tzinfo is None:
            raise ValueError("memory event created_at must include a timezone")
        if not isinstance(self.messages, Sequence) or not self.messages:
            raise ValueError("memory event messages must be a non-empty sequence")
        if not all(isinstance(message, BaseMessage) for message in self.messages):
            raise TypeError("memory event messages must be BaseMessage instances")
        if any(isinstance(message, SystemMessage) for message in self.messages):
            raise ValueError("memory events must not contain system messages")
        object.__setattr__(self, "messages", tuple(self.messages))
