"""A built-in tool for creating and managing session-scoped Cron tasks."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...cron import CronService, CronTask
from ..base import Tool, ToolParameter, ToolResult
from ..context import RequestContext, ToolContext, get_request_context


class CronTool(Tool):
    """Create, list, and remove scheduled tasks for the current session."""

    def __init__(self, cron_service: CronService, timezone: str) -> None:
        if not isinstance(cron_service, CronService):
            raise TypeError("CronTool requires a CronService")
        self._cron_service = cron_service
        self._timezone = _resolve_timezone(timezone)
        super().__init__(
            name="cron",
            description="Create, list, or remove scheduled tasks for this conversation.",
            parameters=(
                ToolParameter(
                    name="action",
                    description="The operation to perform: add, list, or remove.",
                    type="string",
                    required=True,
                ),
                ToolParameter(
                    name="message",
                    description="The message or instruction for a new scheduled task.",
                    type="string",
                ),
                ToolParameter(
                    name="name",
                    description="An optional display name for a new scheduled task.",
                    type="string",
                ),
                ToolParameter(
                    name="at",
                    description=(
                        "An ISO 8601 time for a one-time task. A time without an "
                        "offset uses the configured Cron timezone."
                    ),
                    type="string",
                ),
                ToolParameter(
                    name="every_seconds",
                    description="The positive interval in seconds for a recurring task.",
                    type="number",
                ),
                ToolParameter(
                    name="task_id",
                    description="The task ID to remove, or an optional ID for a new task.",
                    type="string",
                ),
            ),
        )

    @classmethod
    def enabled(cls, context: ToolContext) -> bool:
        return context.cron_service is not None

    @classmethod
    def create(cls, context: ToolContext) -> CronTool:
        if context.cron_service is None:
            raise ValueError("CronTool requires a CronService")
        return cls(context.cron_service, context.cron_timezone)

    async def execute(
        self,
        action: str,
        message: str | None = None,
        name: str | None = None,
        at: str | None = None,
        every_seconds: float | None = None,
        task_id: str | None = None,
    ) -> ToolResult:
        """Apply a Cron action using route data bound to this agent turn."""

        request_context = get_request_context()
        if request_context is None:
            return _tool_error("Cron is only available while processing a user message")
        if not isinstance(action, str) or not action.strip():
            return _tool_error("Action must be one of: add, list, remove")

        normalized_action = action.strip().lower()
        if normalized_action == "add":
            return self._add_task(
                request_context,
                message=message,
                name=name,
                at=at,
                every_seconds=every_seconds,
                task_id=task_id,
            )
        if normalized_action == "list":
            return self._list_tasks(request_context.session_key)
        if normalized_action == "remove":
            return self._remove_task(request_context.session_key, task_id)
        return _tool_error("Unknown action. Use add, list, or remove")

    def _add_task(
        self,
        request_context: RequestContext,
        *,
        message: str | None,
        name: str | None,
        at: str | None,
        every_seconds: float | None,
        task_id: str | None,
    ) -> ToolResult:
        if not isinstance(message, str) or not message.strip():
            return _tool_error("Adding a scheduled task requires a non-empty message")
        if at is not None and every_seconds is not None:
            return _tool_error("Provide exactly one of at or every_seconds")
        if at is None and every_seconds is None:
            return _tool_error("Adding a scheduled task requires at or every_seconds")
        if task_id is not None and not task_id.strip():
            return _tool_error("Task ID must not be blank")

        task_name = name.strip() if isinstance(name, str) and name.strip() else "Scheduled task"
        routing = {
            "session_key": request_context.session_key,
            "channel": request_context.channel,
            "chat_id": request_context.chat_id,
            "sender_id": request_context.sender_id,
            "metadata": _delivery_metadata(request_context.metadata),
        }
        try:
            if at is not None:
                when = _parse_at_time(at, self._timezone)
                if isinstance(when, ToolResult):
                    return when
                task = self._cron_service.add_at(
                    when,
                    task_id=task_id,
                    name=task_name,
                    message=message.strip(),
                    tz=self._timezone.key,
                    **routing,
                )
                return ToolResult(content=f"Created one-time task: {task.id}")

            interval = _parse_interval(every_seconds)
            if isinstance(interval, ToolResult):
                return interval
            task = self._cron_service.add_every(
                interval,
                task_id=task_id,
                name=task_name,
                message=message.strip(),
                tz=self._timezone.key,
                **routing,
            )
            return ToolResult(content=f"Created recurring task: {task.id}")
        except Exception as error:  # noqa: BLE001
            return _tool_error(f"Unable to create scheduled task: {error}")

    def _list_tasks(self, session_key: str) -> ToolResult:
        try:
            tasks = tuple(
                task
                for task in self._cron_service.list_tasks()
                if task.payload.session_key == session_key
            )
        except Exception as error:  # noqa: BLE001
            return _tool_error(f"Unable to list scheduled tasks: {error}")

        if not tasks:
            return ToolResult(content="No scheduled tasks for this session.")
        lines = ["Scheduled tasks:"]
        lines.extend(f"- {task.id}: {task.name} ({_schedule_description(task)})" for task in tasks)
        return ToolResult(content="\n".join(lines))

    def _remove_task(self, session_key: str, task_id: str | None) -> ToolResult:
        if not isinstance(task_id, str) or not task_id.strip():
            return _tool_error("Removing a scheduled task requires task_id")
        normalized_task_id = task_id.strip()
        try:
            task = self._cron_service.get(normalized_task_id)
            if task is None:
                return _tool_error(f"Scheduled task not found: {normalized_task_id}")
            if task.payload.session_key != session_key:
                return _tool_error("Scheduled task does not belong to this session")
            if not self._cron_service.remove(normalized_task_id):
                return _tool_error(f"Scheduled task not found: {normalized_task_id}")
        except Exception as error:  # noqa: BLE001
            return _tool_error(f"Unable to remove scheduled task: {error}")
        return ToolResult(content=f"Removed scheduled task: {normalized_task_id}")


def _resolve_timezone(value: str) -> ZoneInfo:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("CronTool timezone must be a non-empty IANA timezone")
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise ValueError("CronTool timezone must be a valid IANA timezone") from error


def _parse_at_time(value: str, timezone: ZoneInfo) -> datetime | ToolResult:
    if not isinstance(value, str) or not value.strip():
        return _tool_error("At must be a non-empty ISO 8601 timestamp")
    try:
        when = datetime.fromisoformat(value.strip())
    except ValueError:
        return _tool_error("At must use ISO 8601 format, for example 2026-09-03T09:00:00+08:00")
    if when.tzinfo is None or when.utcoffset() is None:
        return when.replace(tzinfo=timezone)
    return when


def _parse_interval(value: float | None) -> timedelta | ToolResult:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        return _tool_error("Every_seconds must be a positive number")
    return timedelta(seconds=value)


def _delivery_metadata(metadata: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(metadata, Mapping):
        return {}
    chat_type = metadata.get("qq_chat_type")
    if isinstance(chat_type, str) and chat_type.strip():
        return {"qq_chat_type": chat_type}
    return {}


def _schedule_description(task: CronTask) -> str:
    if task.schedule.kind == "at":
        return f"once at {task.schedule.at_ms}"
    seconds = (task.schedule.every_ms or 0) / 1_000
    return f"every {seconds:g} seconds"


def _tool_error(message: str) -> ToolResult:
    return ToolResult(content=f"Error: {message}", success=False, error=message)
