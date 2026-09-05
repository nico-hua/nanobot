"""JSON-safe state for one session-scoped continuing goal."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Literal

GoalStatus = Literal["active", "completed", "cancelled", "failed"]
_FINAL_GOAL_STATUSES = frozenset({"completed", "cancelled", "failed"})
_GOAL_START_MESSAGE_TEMPLATE = (
    "Start working on the current sustained goal.\n\n"
    "Goal:\n"
    "{objective}\n\n"
    "Begin from the context saved in the current Session and use the available "
    "tools to make steady progress toward the goal."
)
_GOAL_CONTINUATION_MESSAGE_TEMPLATE = (
    "Continue executing the current sustained goal.\n\n"
    "Goal:\n"
    "{objective}\n\n"
    "Resume from the context saved in the current Session and use the available "
    "tools to make steady progress toward completing the goal."
)


@dataclass(frozen=True)
class GoalState:
    """The one active or terminal goal retained by a session."""

    status: GoalStatus
    objective: str
    created_at: datetime
    updated_at: datetime
    ended_at: datetime | None = None
    continuation_count: int = 0

    def __post_init__(self) -> None:
        if self.status not in {"active", "completed", "cancelled", "failed"}:
            raise ValueError(f"Unknown goal status: {self.status}")
        if not isinstance(self.objective, str) or not self.objective.strip():
            raise ValueError("Goal objective must be a non-empty string")
        _validate_timestamp("created_at", self.created_at)
        _validate_timestamp("updated_at", self.updated_at)
        if self.ended_at is not None:
            _validate_timestamp("ended_at", self.ended_at)
        if not isinstance(self.continuation_count, int) or isinstance(
            self.continuation_count,
            bool,
        ):
            raise TypeError("Goal continuation_count must be an integer")
        if self.continuation_count < 0:
            raise ValueError("Goal continuation_count must not be negative")
        if self.status == "active":
            if self.ended_at is not None:
                raise ValueError("An active goal cannot have ended_at")
        elif self.ended_at is None:
            raise ValueError("A terminal goal requires ended_at")

    @classmethod
    def create(cls, objective: str) -> GoalState:
        """Create a new active goal using the current UTC time."""

        now = datetime.now(timezone.utc)
        return cls(
            status="active",
            objective=objective,
            created_at=now,
            updated_at=now,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> GoalState:
        """Restore a GoalState from the JSON object saved with a session."""

        if not isinstance(value, Mapping):
            raise TypeError("Goal state must be an object")
        return cls(
            status=_goal_status(value.get("status")),
            objective=_required_text(value, "objective"),
            created_at=_timestamp(value, "created_at"),
            updated_at=_timestamp(value, "updated_at"),
            ended_at=_optional_timestamp(value, "ended_at"),
            continuation_count=_continuation_count(value),
        )

    def to_dict(self) -> dict[str, str | int | None]:
        """Return a JSON-safe representation suitable for Session persistence."""

        return {
            "status": self.status,
            "objective": self.objective,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at is not None else None,
            "continuation_count": self.continuation_count,
        }

    def finish(self, status: GoalStatus) -> GoalState:
        """Return this active goal in one terminal state."""

        if self.status != "active":
            raise ValueError("Only an active goal can be finished")
        if status not in _FINAL_GOAL_STATUSES:
            raise ValueError("Goal finish requires a terminal status")
        now = datetime.now(timezone.utc)
        return replace(
            self,
            status=status,
            updated_at=now,
            ended_at=now,
        )

    def replace_objective(self, objective: str) -> GoalState:
        """Return this active goal with a replacement objective."""

        if self.status != "active":
            raise ValueError("Only an active goal can be replaced")
        return replace(
            self,
            objective=objective,
            updated_at=datetime.now(timezone.utc),
        )

    def record_continuation(self) -> GoalState:
        """Return this active goal after one continuation was scheduled."""

        if self.status != "active":
            raise ValueError("Only an active goal can continue")
        return replace(
            self,
            continuation_count=self.continuation_count + 1,
            updated_at=datetime.now(timezone.utc),
        )


def build_goal_start_content(objective: str) -> str:
    """Build the internal instruction that starts work on a saved goal."""

    if not isinstance(objective, str) or not objective.strip():
        raise ValueError("Goal objective must be a non-empty string")
    return _GOAL_START_MESSAGE_TEMPLATE.format(objective=objective.strip())


def build_goal_continuation_content(objective: str) -> str:
    """Build the internal instruction that resumes an active goal."""

    if not isinstance(objective, str) or not objective.strip():
        raise ValueError("Goal objective must be a non-empty string")
    return _GOAL_CONTINUATION_MESSAGE_TEMPLATE.format(objective=objective.strip())


def _goal_status(value: object) -> GoalStatus:
    if not isinstance(value, str) or value not in {
        "active",
        "completed",
        "cancelled",
        "failed",
    }:
        raise ValueError("Goal state status is invalid")
    return value


def _required_text(value: Mapping[str, Any], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"Goal state {name} must be a non-empty string")
    return item


def _timestamp(value: Mapping[str, Any], name: str) -> datetime:
    item = _required_text(value, name)
    try:
        timestamp = datetime.fromisoformat(item)
    except ValueError as error:
        raise ValueError(f"Goal state {name} must be an ISO timestamp") from error
    _validate_timestamp(name, timestamp)
    return timestamp


def _optional_timestamp(value: Mapping[str, Any], name: str) -> datetime | None:
    item = value.get(name)
    if item is None:
        return None
    return _timestamp(value, name)


def _continuation_count(value: Mapping[str, Any]) -> int:
    """Read the optional count so sessions saved before this field still load."""

    count = value.get("continuation_count", 0)
    if not isinstance(count, int) or isinstance(count, bool):
        raise TypeError("Goal state continuation_count must be an integer")
    if count < 0:
        raise ValueError("Goal state continuation_count must not be negative")
    return count


def _validate_timestamp(name: str, value: datetime) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"Goal {name} must be a datetime")
    if value.tzinfo is None:
        raise ValueError(f"Goal {name} must include a timezone")
