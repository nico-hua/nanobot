"""Data model for one persisted Agent conversation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from ..providers import BaseMessage


@dataclass(frozen=True)
class Session:
    """One complete conversation identified by a stable session key."""

    key: str
    created_at: datetime
    updated_at: datetime
    messages: tuple[BaseMessage, ...] = ()

    def __post_init__(self) -> None:
        _validate_session_key(self.key)
        _validate_timestamp("created_at", self.created_at)
        _validate_timestamp("updated_at", self.updated_at)
        if not isinstance(self.messages, Sequence) or not all(
            isinstance(message, BaseMessage) for message in self.messages
        ):
            raise TypeError("messages must be a sequence of BaseMessage instances")
        object.__setattr__(self, "messages", tuple(self.messages))

    @classmethod
    def create(cls, key: str) -> Session:
        """Create a new empty session using the current UTC time."""

        now = datetime.now(timezone.utc)
        return cls(key=key, created_at=now, updated_at=now)

    def with_messages(self, messages: Sequence[BaseMessage]) -> Session:
        """Return a session with a replacement complete message history."""

        return replace(self, messages=tuple(messages))

    def with_updated_at(self, updated_at: datetime) -> Session:
        """Return a session with its persisted update time replaced."""

        return replace(self, updated_at=updated_at)


def _validate_session_key(key: str) -> None:
    if not isinstance(key, str) or not key.strip():
        raise ValueError("session key must be a non-empty string")


def _validate_timestamp(name: str, value: datetime) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
