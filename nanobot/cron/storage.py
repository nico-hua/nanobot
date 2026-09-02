"""Atomic JSON persistence for workspace Cron tasks."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (
    DEFAULT_CRON_TIMEZONE,
    CronJobState,
    CronPayload,
    CronSchedule,
    CronTask,
)

_FORMAT_VERSION = 1


class CronStorageError(RuntimeError):
    """Raised when persisted Cron task data cannot be safely read or written."""


class JsonCronTaskStorage:
    """Store all workspace Cron tasks in one atomically replaced JSON file."""

    def __init__(self, workspace: str | Path) -> None:
        if not isinstance(workspace, (str, Path)):
            raise TypeError("Cron task storage workspace must be a string or Path")
        self._directory = Path(workspace) / "cron"
        self._path = self._directory / "tasks.json"

    @property
    def path(self) -> Path:
        """Return the JSON file used for task persistence."""

        return self._path

    def load(self) -> tuple[CronTask, ...]:
        """Load all tasks, or return an empty tuple before the first save."""

        try:
            content = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ()
        except (OSError, UnicodeError) as error:
            raise CronStorageError("Unable to read Cron task storage") from error
        try:
            document = json.loads(content)
        except json.JSONDecodeError as error:
            raise CronStorageError("Cron task storage contains invalid JSON") from error
        return _tasks_from_document(document)

    def save(self, tasks: Sequence[CronTask]) -> None:
        """Atomically replace the task document with the supplied task state."""

        document = {
            "version": _FORMAT_VERSION,
            "tasks": [_task_to_record(task) for task in tasks],
        }
        try:
            content = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
        except (TypeError, ValueError) as error:
            raise CronStorageError("Cron tasks are not JSON serializable") from error

        temporary_path: Path | None = None
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self._directory,
                prefix=".tasks-",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(content)
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self._path)
        except OSError as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise CronStorageError("Unable to save Cron task storage") from error


def _tasks_from_document(document: Any) -> tuple[CronTask, ...]:
    if not isinstance(document, Mapping):
        raise CronStorageError("Cron task storage must contain a JSON object")
    version = document.get("version")
    if version != _FORMAT_VERSION:
        raise CronStorageError("Cron task storage has an unsupported version")
    records = document.get("tasks")
    if not isinstance(records, list):
        raise CronStorageError("Cron task storage tasks must be a list")

    tasks = tuple(_task_from_record(record) for record in records)
    identifiers = [task.id for task in tasks]
    if len(identifiers) != len(set(identifiers)):
        raise CronStorageError("Cron task storage contains duplicate task IDs")
    return tasks


def _task_to_record(task: CronTask) -> dict[str, Any]:
    if not isinstance(task, CronTask):
        raise TypeError("Cron task storage requires CronTask instances")
    return {
        "id": task.id,
        "name": task.name,
        "enabled": task.enabled,
        "schedule": {
            "kind": task.schedule.kind,
            "at_ms": task.schedule.at_ms,
            "every_ms": task.schedule.every_ms,
            "tz": task.schedule.tz,
        },
        "payload": {
            "message": task.payload.message,
            "session_key": task.payload.session_key,
            "channel": task.payload.channel,
            "chat_id": task.payload.chat_id,
            "sender_id": task.payload.sender_id,
            "metadata": dict(task.payload.metadata),
        },
        "state": {
            "next_run_at": task.state.next_run_at,
            "last_run_at": task.state.last_run_at,
            "last_status": task.state.last_status,
            "last_error": task.state.last_error,
        },
    }


def _task_from_record(record: Any) -> CronTask:
    if not isinstance(record, Mapping):
        raise CronStorageError("Cron task records must be JSON objects")
    schedule = _required_mapping(record, "schedule")
    payload = _required_mapping(record, "payload")
    state = _required_mapping(record, "state")
    kind = schedule.get("kind")
    if kind not in ("at", "every"):
        raise CronStorageError("Cron task schedule kind must be 'at' or 'every'")

    at_ms = _optional_integer(schedule, "at_ms")
    every_ms = _optional_integer(schedule, "every_ms")
    if kind == "at" and at_ms is None:
        raise CronStorageError("One-time Cron tasks require an at_ms")
    if kind == "at" and every_ms is not None:
        raise CronStorageError("One-time Cron tasks must not define an every_ms")
    if kind == "every" and at_ms is not None:
        raise CronStorageError("Recurring Cron tasks must not define an at_ms")
    if kind == "every" and (every_ms is None or every_ms <= 0):
        raise CronStorageError("Recurring Cron tasks require a positive every_ms")

    enabled = _required_bool(record, "enabled")
    next_run_at = _optional_integer(state, "next_run_at")
    if enabled and next_run_at is None:
        raise CronStorageError("Enabled Cron tasks require a next_run_at")
    return CronTask(
        id=_required_text(record, "id"),
        schedule=CronSchedule(
            kind=kind,
            at_ms=at_ms,
            every_ms=every_ms,
            tz=_timezone_from_record(schedule),
        ),
        payload=CronPayload(
            message=_optional_string(payload, "message", default=""),
            session_key=_optional_string(payload, "session_key", default=""),
            channel=_optional_nullable_string(payload, "channel"),
            chat_id=_optional_nullable_string(payload, "chat_id"),
            sender_id=_optional_string(payload, "sender_id", default="cron"),
            metadata=_optional_mapping(payload, "metadata"),
        ),
        name=_optional_string(record, "name", default=""),
        enabled=enabled,
        state=CronJobState(
            next_run_at=next_run_at,
            last_run_at=_optional_integer(state, "last_run_at"),
            last_status=_optional_nullable_string(state, "last_status"),
            last_error=_optional_nullable_string(state, "last_error"),
        ),
    )


def _optional_integer(record: Mapping[str, Any], name: str) -> int | None:
    value = record.get(name)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise CronStorageError(f"Cron task {name} must be an integer or null")
    return value


def _optional_mapping(record: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = record.get(name, {})
    if not isinstance(value, Mapping):
        raise CronStorageError(f"Cron task {name} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise CronStorageError(f"Cron task {name} keys must be strings")
    return dict(value)


def _timezone_from_record(record: Mapping[str, Any]) -> str:
    value = _optional_string(record, "tz", default=DEFAULT_CRON_TIMEZONE)
    if not value.strip():
        raise CronStorageError("Cron task timezone must not be blank")
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise CronStorageError("Cron task timezone must be a valid IANA timezone") from error
    return value


def _required_mapping(record: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = record.get(name)
    if not isinstance(value, Mapping):
        raise CronStorageError(f"Cron task {name} must be an object")
    return value


def _required_bool(record: Mapping[str, Any], name: str) -> bool:
    value = record.get(name)
    if not isinstance(value, bool):
        raise CronStorageError(f"Cron task {name} must be a boolean")
    return value


def _required_text(record: Mapping[str, Any], name: str) -> str:
    value = _optional_string(record, name, default=None)
    if value is None or not value.strip():
        raise CronStorageError(f"Cron task {name} must be a non-empty string")
    return value


def _optional_string(
    record: Mapping[str, Any],
    name: str,
    *,
    default: str | None,
) -> str | None:
    value = record.get(name, default)
    if not isinstance(value, str):
        raise CronStorageError(f"Cron task {name} must be a string")
    return value


def _optional_nullable_string(record: Mapping[str, Any], name: str) -> str | None:
    value = record.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CronStorageError(f"Cron task {name} must be a string or null")
    return value
