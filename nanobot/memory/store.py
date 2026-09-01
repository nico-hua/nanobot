"""Access workspace long-term memory, durable events, and the processing cursor."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from ..providers import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolCallRequest,
    ToolMessage,
)
from .models import MemoryEvent

logger = logging.getLogger(__name__)


class MemoryStore:
    """Persist one workspace's memory file, event queue, and cursor."""

    def __init__(self, workspace: str | Path) -> None:
        if not isinstance(workspace, (str, Path)):
            raise TypeError("MemoryStore workspace must be a string or Path")
        self._directory = Path(workspace).resolve() / "memory"
        self._memory_path = self._directory / "MEMORY.md"
        self._history_path = self._directory / "history.jsonl"
        self._cursor_path = self._directory / ".memory_cursor"

    def read(self) -> str:
        """Return the current UTF-8 memory content, or an empty string when unavailable."""

        try:
            content = self._memory_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        except (OSError, UnicodeDecodeError):
            logger.warning("Skipping unreadable long-term memory file")
            return ""
        return content if content.strip() else ""

    def write(self, content: str) -> None:
        """Atomically replace the memory file with non-empty UTF-8 content."""

        if not isinstance(content, str):
            raise TypeError("MemoryStore content must be a string")
        if not content.strip():
            raise ValueError("MemoryStore content must not be blank")

        temporary_path: Path | None = None
        try:
            self._memory_path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._memory_path.parent,
                prefix=f".{self._memory_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(content)
            temporary_path.replace(self._memory_path)
        except (OSError, UnicodeError):
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    def append_event(
        self,
        session_key: str,
        messages: Sequence[BaseMessage],
    ) -> MemoryEvent:
        """Append one completed-turn snapshot to the durable memory queue."""

        events = self._read_events()
        event = MemoryEvent(
            event_id=events[-1].event_id + 1 if events else 1,
            session_key=session_key,
            created_at=datetime.now(timezone.utc),
            messages=tuple(messages),
        )
        self._directory.mkdir(parents=True, exist_ok=True)
        record = _event_to_record(event)
        try:
            with self._history_path.open("a", encoding="utf-8", newline="\n") as file:
                file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                file.write("\n")
                file.flush()
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            raise OSError("Unable to append memory event") from exc
        return event

    def read_events_after(self, cursor: int) -> tuple[MemoryEvent, ...]:
        """Return durable memory events with IDs greater than ``cursor``."""

        _validate_cursor(cursor)
        return tuple(event for event in self._read_events() if event.event_id > cursor)

    def read_cursor(self) -> int:
        """Return the last successfully processed memory event ID."""

        try:
            content = self._cursor_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return 0
        except (OSError, UnicodeDecodeError) as exc:
            raise OSError("Unable to read memory cursor") from exc
        try:
            cursor = int(content)
        except ValueError as exc:
            raise ValueError("Memory cursor must be an integer") from exc
        _validate_cursor(cursor)
        return cursor

    def update_cursor(self, event_id: int) -> None:
        """Atomically record the last successfully processed memory event ID."""

        _validate_cursor(event_id)
        current_cursor = self.read_cursor()
        if event_id < current_cursor:
            raise ValueError("Memory cursor must not move backwards")
        self._replace_text(self._cursor_path, f"{event_id}\n")

    def _read_events(self) -> tuple[MemoryEvent, ...]:
        try:
            with self._history_path.open(encoding="utf-8") as file:
                records = [
                    _record_from_line(line, line_number)
                    for line_number, line in enumerate(file, start=1)
                    if line.strip()
                ]
        except FileNotFoundError:
            return ()
        except (OSError, UnicodeDecodeError) as exc:
            raise OSError("Unable to read memory event history") from exc

        events = tuple(_event_from_record(record) for record in records)
        previous_id = 0
        for event in events:
            if event.event_id <= previous_id:
                raise ValueError("Memory event IDs must be strictly increasing")
            previous_id = event.event_id
        return events

    def _replace_text(self, path: Path, content: str) -> None:
        temporary_path: Path | None = None
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._directory,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(content)
            temporary_path.replace(path)
        except (OSError, UnicodeError):
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise


def _event_to_record(event: MemoryEvent) -> dict[str, Any]:
    return {
        "type": "memory_event",
        "event_id": event.event_id,
        "session_key": event.session_key,
        "created_at": event.created_at.isoformat(),
        "messages": [_message_to_record(message) for message in event.messages],
    }


def _event_from_record(record: Mapping[str, Any]) -> MemoryEvent:
    if record.get("type") != "memory_event":
        raise ValueError("Invalid memory event record")
    event_id = record.get("event_id")
    if not isinstance(event_id, int) or isinstance(event_id, bool):
        raise TypeError("Memory event ID must be an integer")
    session_key = _required_text(record, "session_key")
    created_at = _timestamp_from_record(record, "created_at")
    message_records = record.get("messages")
    if not isinstance(message_records, list):
        raise TypeError("Memory event messages must be a list")
    return MemoryEvent(
        event_id=event_id,
        session_key=session_key,
        created_at=created_at,
        messages=tuple(_message_from_record(message) for message in message_records),
    )


def _message_to_record(message: BaseMessage) -> dict[str, Any]:
    record: dict[str, Any] = {
        "role": message.role,
        "content": message.content,
    }
    if isinstance(message, AIMessage):
        record["tool_calls"] = [
            {
                "id": tool_call.id,
                "name": tool_call.name,
                "arguments": dict(tool_call.arguments),
            }
            for tool_call in message.tool_calls
        ]
    elif isinstance(message, ToolMessage):
        record["tool_call_id"] = message.tool_call_id
    elif not isinstance(message, HumanMessage):
        raise TypeError(f"Unsupported memory event message type: {type(message).__name__}")
    return record


def _message_from_record(record: Any) -> BaseMessage:
    if not isinstance(record, Mapping):
        raise TypeError("Memory event message must be an object")
    content = _required_string(record, "content")
    role = _required_text(record, "role")
    if role == "user":
        return HumanMessage(content=content)
    if role == "assistant":
        tool_calls = record.get("tool_calls", [])
        if not isinstance(tool_calls, list):
            raise ValueError("Memory event tool_calls must be a list")
        return AIMessage(
            content=content,
            tool_calls=tuple(_tool_call_from_record(tool_call) for tool_call in tool_calls),
        )
    if role == "tool":
        return ToolMessage(
            content=content,
            tool_call_id=_required_text(record, "tool_call_id"),
        )
    raise ValueError(f"Unsupported memory event message role: {role}")


def _tool_call_from_record(record: Any) -> ToolCallRequest:
    if not isinstance(record, Mapping):
        raise TypeError("Memory event tool call must be an object")
    arguments = record.get("arguments")
    if not isinstance(arguments, Mapping):
        raise TypeError("Memory event tool call arguments must be an object")
    return ToolCallRequest(
        id=_required_text(record, "id"),
        name=_required_text(record, "name"),
        arguments=dict(arguments),
    )


def _record_from_line(line: str, line_number: int) -> dict[str, Any]:
    try:
        record = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in memory history at line {line_number}") from exc
    if not isinstance(record, dict):
        raise TypeError(f"Invalid memory history record at line {line_number}")
    return record


def _required_text(record: Mapping[str, Any], name: str) -> str:
    value = _required_string(record, name)
    if not value.strip():
        raise ValueError(f"Memory event {name} must be a non-empty string")
    return value


def _required_string(record: Mapping[str, Any], name: str) -> str:
    value = record.get(name)
    if not isinstance(value, str):
        raise TypeError(f"Memory event {name} must be a string")
    return value


def _timestamp_from_record(record: Mapping[str, Any], name: str) -> datetime:
    value = _required_text(record, name)
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Memory event {name} must be an ISO timestamp") from exc
    if timestamp.tzinfo is None:
        raise ValueError(f"Memory event {name} must include a timezone")
    return timestamp


def _validate_cursor(cursor: int) -> None:
    if not isinstance(cursor, int) or isinstance(cursor, bool):
        raise TypeError("Memory cursor must be an integer")
    if cursor < 0:
        raise ValueError("Memory cursor must not be negative")
