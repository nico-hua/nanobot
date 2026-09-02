"""In-memory scheduling for one-time and recurring tasks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (
    DEFAULT_CRON_TIMEZONE,
    CronJobState,
    CronPayload,
    CronSchedule,
    CronTask,
)
from .storage import CronStorageError, JsonCronTaskStorage

logger = logging.getLogger(__name__)

CronCallback = Callable[[CronTask], Awaitable[None]]


class CronService:
    """Manage in-memory UTC tasks and invoke one shared callback when due."""

    def __init__(self, callback: CronCallback, workspace: str | Path) -> None:
        if not callable(callback):
            raise TypeError("CronService callback must be callable")
        self._callback = callback
        self._storage = JsonCronTaskStorage(workspace)
        self._tasks: dict[str, CronTask] = {}
        self._wakeup = asyncio.Event()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._loaded = False

    @property
    def is_running(self) -> bool:
        """Return whether the background scheduler is currently running."""

        return self._scheduler_task is not None and not self._scheduler_task.done()

    @property
    def storage_path(self) -> Path:
        """Return the task JSON file used by this scheduler."""

        return self._storage.path

    def add_at(
        self,
        when: datetime,
        *,
        task_id: str | None = None,
        name: str = "",
        message: str = "",
        session_key: str = "",
        channel: str | None = None,
        chat_id: str | None = None,
        sender_id: str = "cron",
        metadata: Mapping[str, Any] | None = None,
        tz: str = DEFAULT_CRON_TIMEZONE,
    ) -> CronTask:
        """Register one task to run once at an aware datetime."""

        at_time = _normalize_datetime(when)
        at_ms = _datetime_to_milliseconds(at_time)
        _validate_timezone(tz)
        return self._add_task(
            schedule=CronSchedule(
                kind="at",
                at_ms=at_ms,
                tz=tz,
            ),
            payload=CronPayload(
                message=message,
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
                sender_id=sender_id,
                metadata=_copy_metadata(metadata),
            ),
            task_id=task_id,
            name=name,
            next_run_at=at_ms,
        )

    def add_every(
        self,
        interval: timedelta,
        *,
        task_id: str | None = None,
        start_at: datetime | None = None,
        name: str = "",
        message: str = "",
        session_key: str = "",
        channel: str | None = None,
        chat_id: str | None = None,
        sender_id: str = "cron",
        metadata: Mapping[str, Any] | None = None,
        tz: str = DEFAULT_CRON_TIMEZONE,
    ) -> CronTask:
        """Register one task to run repeatedly after a positive interval."""

        if not isinstance(interval, timedelta):
            raise TypeError("Cron task interval must be a timedelta")
        if interval.total_seconds() <= 0:
            raise ValueError("Cron task interval must be positive")
        _validate_timezone(tz)
        every_ms = _timedelta_to_milliseconds(interval)
        next_run_at = (
            _datetime_to_milliseconds(_normalize_datetime(start_at))
            if start_at is not None
            else _utc_now_ms() + every_ms
        )
        return self._add_task(
            schedule=CronSchedule(
                kind="every",
                every_ms=every_ms,
                tz=tz,
            ),
            payload=CronPayload(
                message=message,
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
                sender_id=sender_id,
                metadata=_copy_metadata(metadata),
            ),
            task_id=task_id,
            name=name,
            next_run_at=next_run_at,
        )

    def get(self, task_id: str) -> CronTask | None:
        """Return one task by ID, or ``None`` when it is not registered."""

        return self._tasks.get(task_id)

    def list_tasks(self) -> tuple[CronTask, ...]:
        """Return registered tasks in their stable registration order."""

        return tuple(self._tasks.values())

    def remove(self, task_id: str) -> bool:
        """Remove one task and report whether it was registered."""

        self._ensure_loaded()
        if task_id not in self._tasks:
            return False
        tasks = self._tasks.copy()
        del tasks[task_id]
        self._save_tasks(tasks)
        self._tasks = tasks
        self._wakeup.set()
        return True

    async def start(self) -> None:
        """Start the one background scheduler task without blocking on it."""

        if self.is_running:
            return
        self._ensure_loaded()
        self._stopping = False
        self._scheduler_task = asyncio.create_task(
            self._run(),
            name="nanobot-cron-service",
        )
        logger.info("Cron service started")

    async def stop(self) -> None:
        """Cancel and await the scheduler task without leaving background work."""

        task = self._scheduler_task
        self._scheduler_task = None
        self._stopping = True
        self._wakeup.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._loaded or self._tasks:
            self._save_tasks(self._tasks)
        logger.info("Cron service stopped")

    def _add_task(
        self,
        *,
        schedule: CronSchedule,
        payload: CronPayload,
        task_id: str | None,
        name: str,
        next_run_at: int,
    ) -> CronTask:
        self._ensure_loaded()
        identifier = _normalize_task_id(task_id)
        if identifier in self._tasks:
            raise ValueError(f"Cron task already exists: {identifier}")
        _validate_task_text(name, "name")
        _validate_payload(payload)
        task = CronTask(
            id=identifier,
            schedule=schedule,
            payload=payload,
            name=name or identifier,
            state=CronJobState(next_run_at=next_run_at),
        )
        tasks = self._tasks.copy()
        tasks[identifier] = task
        self._save_tasks(tasks)
        self._tasks = tasks
        self._wakeup.set()
        return task

    async def _run(self) -> None:
        try:
            while not self._stopping:
                self._wakeup.clear()
                due_tasks = self._due_tasks(_utc_now_ms())
                if due_tasks:
                    for task in due_tasks:
                        if self._stopping:
                            break
                        if self._tasks.get(task.id) is task:
                            await self._execute_task(task)
                    continue
                await self._wait_for_next_task()
        except asyncio.CancelledError:  # noqa: TRY203
            raise

    def _due_tasks(self, now: int) -> tuple[CronTask, ...]:
        return tuple(
            task
            for task in self._tasks.values()
            if (
                task.enabled
                and task.state.next_run_at is not None
                and task.state.next_run_at <= now
            )
        )

    async def _wait_for_next_task(self) -> None:
        next_runs = tuple(
            task.state.next_run_at
            for task in self._tasks.values()
            if task.enabled and task.state.next_run_at is not None
        )
        if not next_runs:
            await self._wakeup.wait()
            return
        next_run_at = min(next_runs)
        delay = max((next_run_at - _utc_now_ms()) / 1000, 0)
        try:
            await asyncio.wait_for(self._wakeup.wait(), timeout=delay)
        except TimeoutError:
            pass

    async def _execute_task(self, task: CronTask) -> None:
        last_error: str | None = None
        try:
            await self._callback(task)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            last_error = type(error).__name__
            logger.exception(
                "Cron task callback failed (task_id=%s, kind=%s)",
                task.id,
                task.schedule.kind,
            )
        finally:
            if self._tasks.get(task.id) is task:
                self._record_execution(task, last_error)

    def _record_execution(self, task: CronTask, last_error: str | None) -> None:
        now = _utc_now_ms()
        task.state.last_run_at = now
        task.state.last_error = last_error
        task.state.last_status = "error" if last_error is not None else "success"
        if task.schedule.kind == "at":
            task.enabled = False
            task.state.next_run_at = None
        elif task.schedule.every_ms is not None and task.enabled:
            task.state.next_run_at = now + task.schedule.every_ms
        else:
            task.state.next_run_at = None
        try:
            self._save_tasks(self._tasks)
        except CronStorageError:
            logger.exception("Unable to persist Cron task execution state (task_id=%s)", task.id)
        self._wakeup.set()

    def _load_tasks(self) -> None:
        tasks = self._storage.load()
        self._tasks = {task.id: task for task in tasks}
        self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._load_tasks()

    def _save_tasks(self, tasks: dict[str, CronTask]) -> None:
        self._storage.save(tuple(tasks.values()))


def _utc_now_ms() -> int:
    return _datetime_to_milliseconds(datetime.now(timezone.utc))


def _normalize_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("Cron task time must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Cron task time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _datetime_to_milliseconds(value: datetime) -> int:
    return round(value.timestamp() * 1000)


def _timedelta_to_milliseconds(value: timedelta) -> int:
    milliseconds = round(value.total_seconds() * 1000)
    if milliseconds <= 0:
        raise ValueError("Cron task interval must be at least one millisecond")
    return milliseconds


def _validate_timezone(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Cron task timezone must be a non-empty IANA timezone")
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise ValueError("Cron task timezone must be a valid IANA timezone") from error


def _normalize_task_id(task_id: str | None) -> str:
    if task_id is None:
        return uuid4().hex
    if not isinstance(task_id, str):
        raise TypeError("Cron task ID must be a string")
    normalized_task_id = task_id.strip()
    if not normalized_task_id:
        raise ValueError("Cron task ID must not be blank")
    return normalized_task_id


def _validate_task_text(value: str, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"Cron task {name} must be a string")


def _validate_payload(payload: CronPayload) -> None:
    _validate_task_text(payload.message, "message")
    _validate_task_text(payload.session_key, "session_key")
    _validate_task_text(payload.sender_id, "sender_id")
    if not isinstance(payload.metadata, Mapping):
        raise TypeError("Cron task metadata must be a mapping")
    if not all(isinstance(key, str) for key in payload.metadata):
        raise TypeError("Cron task metadata keys must be strings")


def _copy_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise TypeError("Cron task metadata must be a mapping")
    if not all(isinstance(key, str) for key in metadata):
        raise TypeError("Cron task metadata keys must be strings")
    return dict(metadata)
