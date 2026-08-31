"""LLM-backed consolidation of durable long-term memory."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from typing import Any

from ..providers import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    LLMProvider,
    SystemMessage,
    ToolMessage,
)
from .store import MemoryStore

logger = logging.getLogger(__name__)

_CONSOLIDATION_INSTRUCTIONS = """Update the long-term memory for a future Agent.
Extract only stable, high-value information: enduring user preferences, confirmed
project conventions, important facts, and decisions needed by future tasks. Ignore
small talk, temporary status, one-off task details, duplicates, and sensitive data.
Return only the complete replacement MEMORY.md content. If there is no durable
information to retain, return an empty response. Do not call tools."""


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
            return await self._consolidate(conversation)

    async def _consolidate(self, messages: tuple[BaseMessage, ...]) -> bool:
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
            return False

        content = (response.content or "").strip()
        if not content or response.tool_calls:
            logger.warning("Long-term memory consolidation returned no usable content")
            return False

        try:
            self._memory_store.write(content)
        except (OSError, UnicodeError):
            logger.exception("Long-term memory update failed")
            return False
        return True


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
