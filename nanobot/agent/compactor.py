"""LLM-backed summary compaction for complete persisted sessions."""

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
from ..session import Session
from .context import _split_user_turns, estimate_messages_tokens

logger = logging.getLogger(__name__)

DEFAULT_COMPACTION_THRESHOLD_TOKENS = 64_000
DEFAULT_COMPACTION_RECENT_TOKENS = 32_000
_SUMMARY_INSTRUCTIONS = """Summarize this conversation for a future assistant turn.
Preserve user goals, facts, decisions, constraints, unresolved questions, and useful
tool interactions. Do not invent details. Return only the concise conversation summary."""


class SessionCompactor:
    """Replace older complete conversation turns with one persisted summary context."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        token_threshold: int = DEFAULT_COMPACTION_THRESHOLD_TOKENS,
        recent_token_budget: int = DEFAULT_COMPACTION_RECENT_TOKENS,
    ) -> None:
        if not isinstance(provider, LLMProvider):
            raise TypeError("SessionCompactor requires an LLMProvider")
        _validate_token_budget("token_threshold", token_threshold, positive=True)
        _validate_token_budget("recent_token_budget", recent_token_budget, positive=False)
        if recent_token_budget >= token_threshold:
            raise ValueError("recent_token_budget must be less than token_threshold")

        self._provider = provider
        self._token_threshold = token_threshold
        self._recent_token_budget = recent_token_budget

    def should_compact(self, session: Session) -> bool:
        """Return whether raw messages after the current summary exceed the threshold."""

        if not isinstance(session, Session):
            raise TypeError("SessionCompactor requires a Session")
        return (
            estimate_messages_tokens(session.messages[session.summary_until :])
            >= self._token_threshold
        )

    async def compact(self, session: Session) -> Session:
        """Return a session with an updated summary when a complete prefix can be compacted."""

        if not self.should_compact(session):
            return session

        messages_to_summarize, summary_until = self._select_messages(session)
        if not messages_to_summarize:
            return session

        try:
            response = await self._provider.complete(
                (
                    SystemMessage(content=_SUMMARY_INSTRUCTIONS),
                    HumanMessage(
                        content=_summary_request_content(
                            session.summary,
                            messages_to_summarize,
                        )
                    ),
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("Session compaction failed while requesting a summary")
            return session

        summary = (response.content or "").strip()
        if not summary or response.tool_calls:
            logger.warning("Session compaction returned no usable summary")
            return session
        return session.with_summary(summary, summary_until)

    def _select_messages(self, session: Session) -> tuple[tuple[BaseMessage, ...], int]:
        raw_messages = session.messages[session.summary_until :]
        turns = _split_user_turns(raw_messages)
        if not turns or sum(len(turn) for turn in turns) != len(raw_messages):
            return (), session.summary_until

        retained_turn_count = 0
        remaining_tokens = self._recent_token_budget
        for turn in reversed(turns):
            turn_tokens = estimate_messages_tokens(turn)
            if retained_turn_count and turn_tokens > remaining_tokens:
                break
            retained_turn_count += 1
            remaining_tokens = max(0, remaining_tokens - turn_tokens)

        compacted_turns = turns[: len(turns) - retained_turn_count]
        if not compacted_turns:
            return (), session.summary_until
        messages_to_summarize = tuple(
            message for turn in compacted_turns for message in turn
        )
        return messages_to_summarize, session.summary_until + len(messages_to_summarize)


def _summary_request_content(
    previous_summary: str | None,
    messages: Sequence[BaseMessage],
) -> str:
    request: dict[str, Any] = {
        "messages": [_message_to_summary_record(message) for message in messages],
    }
    if previous_summary is not None:
        request["previous_summary"] = previous_summary
    return json.dumps(request, ensure_ascii=False, separators=(",", ":"), default=str)


def _message_to_summary_record(message: BaseMessage) -> dict[str, Any]:
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


def _validate_token_budget(name: str, value: int, *, positive: bool) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if value < 0 or (positive and value == 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be {qualifier}")
