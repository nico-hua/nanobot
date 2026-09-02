"""LLM-backed consolidation of durable long-term memory."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from enum import Enum
from typing import Any

from ..providers import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    LLMProvider,
    SystemMessage,
    ToolMessage,
)
from .models import MemoryEvent
from .store import MemoryStore

logger = logging.getLogger(__name__)

_CONSOLIDATION_INSTRUCTIONS = """Maintain the long-term memory for a future Agent.
Keep only stable, high-value information from the existing memory and completed
conversation: confirmed user facts, enduring preferences, project conventions, and
important decisions needed by later work. Ignore small talk, temporary status,
one-off task details, duplicates, and sensitive information.

Return only the complete replacement MEMORY.md content. Use only these Markdown
sections, omitting a section when it has no content:

## User Information
## Preferences
## Project Context
## Important Notes

When there is durable information, include both applicable existing memory and new
confirmed facts in the replacement so no valid prior memory is lost. If neither the
existing memory nor this conversation contains durable information to retain, return
an empty response. Reprocessing the same conversation must not duplicate facts. Do
not call tools."""


class MemoryConsolidationOutcome(Enum):
    """The result of processing one durable memory event."""

    UPDATED = "updated"
    SKIPPED = "skipped"
    FAILED = "failed"


class MemoryConsolidator:
    """Serialize LLM-backed long-term memory updates for one workspace."""

    def __init__(self, provider: LLMProvider, memory_store: MemoryStore) -> None:
        if not isinstance(provider, LLMProvider):
            raise TypeError("MemoryConsolidator requires an LLMProvider")
        if not isinstance(memory_store, MemoryStore):
            raise TypeError("MemoryConsolidator requires a MemoryStore")
        self._provider = provider
        self._memory_store = memory_store
        self._workspace_lock = asyncio.Lock()

    async def consolidate(self, messages: Sequence[BaseMessage]) -> bool:
        """Persist one valid replacement memory generated from completed messages."""

        _validate_messages(messages)
        conversation = tuple(messages)
        if not conversation:
            return False

        async with self._workspace_lock:
            outcome = await self._consolidate(conversation)
        return outcome is MemoryConsolidationOutcome.UPDATED

    async def consolidate_event(self, event: MemoryEvent) -> MemoryConsolidationOutcome:
        """Consolidate one durable event without appending duplicate memory content."""

        if not isinstance(event, MemoryEvent):
            raise TypeError("MemoryConsolidator requires a MemoryEvent")
        return await self.consolidate_events((event,))

    async def consolidate_events(
        self,
        events: Sequence[MemoryEvent],
    ) -> MemoryConsolidationOutcome:
        """Consolidate one ordered batch of durable events in a single request."""

        if not isinstance(events, Sequence) or not events:
            raise ValueError("MemoryConsolidator requires at least one MemoryEvent")
        if not all(isinstance(event, MemoryEvent) for event in events):
            raise TypeError("MemoryConsolidator requires MemoryEvent instances")

        messages = tuple(
            message
            for event in events
            for message in event.messages
        )
        async with self._workspace_lock:
            return await self._consolidate(messages)

    async def _consolidate(
        self,
        messages: tuple[BaseMessage, ...],
    ) -> MemoryConsolidationOutcome:
        request = HumanMessage(
            content=_consolidation_request_content(
                self._memory_store.read(),
                messages,
            )
        )
        try:
            response = await self._provider.complete(
                (SystemMessage(content=_CONSOLIDATION_INSTRUCTIONS), request)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Long-term memory consolidation request failed")
            return MemoryConsolidationOutcome.FAILED

        content = (response.content or "").strip()
        if not content:
            logger.debug("Long-term memory consolidation found no durable information")
            return MemoryConsolidationOutcome.SKIPPED
        if response.tool_calls:
            logger.warning("Long-term memory consolidation returned no usable content")
            return MemoryConsolidationOutcome.FAILED

        try:
            self._memory_store.write(content)
        except (OSError, UnicodeError):
            logger.exception("Long-term memory update failed")
            return MemoryConsolidationOutcome.FAILED
        return MemoryConsolidationOutcome.UPDATED


def _consolidation_request_content(
    existing_memory: str,
    messages: Sequence[BaseMessage],
) -> str:
    request = {
        "existing_memory": existing_memory,
        "completed_messages": [_message_record(message) for message in messages],
    }
    return json.dumps(request, ensure_ascii=False, separators=(",", ":"), default=str)


def _message_record(message: BaseMessage) -> dict[str, Any]:
    record: dict[str, Any] = {"role": message.role, "content": message.content}
    if isinstance(message, AIMessage) and message.tool_calls:
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
    return record


def _validate_messages(messages: Sequence[BaseMessage]) -> None:
    if not isinstance(messages, Sequence) or not all(
        isinstance(message, BaseMessage) for message in messages
    ):
        raise TypeError("messages must be a sequence of BaseMessage instances")
