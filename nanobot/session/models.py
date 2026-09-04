"""Data model for one persisted Agent conversation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from ..providers import BaseMessage
from .goals import GoalState


@dataclass(frozen=True)
class Session:
    """One complete conversation identified by a stable session key."""

    key: str
    created_at: datetime
    updated_at: datetime
    messages: tuple[BaseMessage, ...] = ()
    summary: str | None = None
    summary_until: int = 0
    goal_state: GoalState | None = None

    def __post_init__(self) -> None:
        _validate_session_key(self.key)
        _validate_timestamp("created_at", self.created_at)
        _validate_timestamp("updated_at", self.updated_at)
        if not isinstance(self.messages, Sequence) or not all(
            isinstance(message, BaseMessage) for message in self.messages
        ):
            raise TypeError("messages must be a sequence of BaseMessage instances")
        object.__setattr__(self, "messages", tuple(self.messages))
        if self.summary is not None:
            if not isinstance(self.summary, str):
                raise TypeError("summary must be a string or None")
            if not self.summary.strip():
                raise ValueError("summary must not be blank")
        if not isinstance(self.summary_until, int) or isinstance(self.summary_until, bool):
            raise TypeError("summary_until must be an integer")
        if self.summary_until < 0 or self.summary_until > len(self.messages):
            raise ValueError("summary_until must reference the session message range")
        if self.summary is None and self.summary_until:
            raise ValueError("summary_until requires a summary")
        if self.goal_state is not None and not isinstance(self.goal_state, GoalState):
            raise TypeError("goal_state must be a GoalState or None")

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

    def with_summary(self, summary: str, summary_until: int) -> Session:
        """Return a session with one summary and its covered message boundary."""

        return replace(self, summary=summary, summary_until=summary_until)

    def with_goal_state(self, goal_state: GoalState | None) -> Session:
        """Return a session with its dedicated goal state replaced."""

        return replace(self, goal_state=goal_state)

    def reset(self) -> Session:
        """Return the same session identity with its short-term state and goal cleared."""

        return replace(self, messages=(), summary=None, summary_until=0, goal_state=None)


def _validate_session_key(key: str) -> None:
    if not isinstance(key, str) or not key.strip():
        raise ValueError("session key must be a non-empty string")


def _validate_timestamp(name: str, value: datetime) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
