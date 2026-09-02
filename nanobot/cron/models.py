"""Structured models for persistable Cron tasks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

CronTaskKind = Literal["at", "every"]
DEFAULT_CRON_TIMEZONE = "Asia/Shanghai"


@dataclass
class CronSchedule:
    """The time-based definition of one Cron task."""

    kind: CronTaskKind
    at_ms: int | None = None
    every_ms: int | None = None
    tz: str = DEFAULT_CRON_TIMEZONE


@dataclass
class CronPayload:
    """The message and routing information used when a task runs."""

    message: str = ""
    session_key: str = ""
    channel: str | None = None
    chat_id: str | None = None
    sender_id: str = "cron"
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class CronJobState:
    """The mutable execution state kept separately from task definition."""

    next_run_at: int | None = None
    last_run_at: int | None = None
    last_status: str | None = None
    last_error: str | None = None


@dataclass
class CronTask:
    """One persistable Cron task with schedule, payload, and run state."""

    id: str
    schedule: CronSchedule
    payload: CronPayload
    name: str = ""
    enabled: bool = True
    state: CronJobState = field(default_factory=CronJobState)
