"""Token estimation and turn grouping for persisted session messages."""

from __future__ import annotations

import json
from collections.abc import Sequence

from ..providers import AIMessage, BaseMessage, HumanMessage, ToolMessage

_MESSAGE_OVERHEAD_TOKENS = 4


def estimate_message_tokens(message: BaseMessage) -> int:
    """Return a stable, dependency-free approximate token count for one message."""

    if not isinstance(message, BaseMessage):
        raise TypeError("token estimation requires a BaseMessage")

    tokens = _MESSAGE_OVERHEAD_TOKENS + _estimate_text_tokens(message.role)
    tokens += _estimate_text_tokens(message.content)
    if isinstance(message, AIMessage) and message.tool_calls:
        tool_calls = [
            {
                "id": tool_call.id,
                "name": tool_call.name,
                "arguments": dict(tool_call.arguments),
            }
            for tool_call in message.tool_calls
        ]
        tokens += _estimate_text_tokens(
            json.dumps(
                tool_calls,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                default=str,
            )
        )
    elif isinstance(message, ToolMessage):
        tokens += _estimate_text_tokens(message.tool_call_id)
    return max(1, tokens)


def estimate_messages_tokens(messages: Sequence[BaseMessage]) -> int:
    """Return the summed approximate token count for a message sequence."""

    _validate_messages(messages)
    return sum(estimate_message_tokens(message) for message in messages)


def split_user_turns(
    history: tuple[BaseMessage, ...],
) -> tuple[tuple[BaseMessage, ...], ...]:
    """Group messages from each user message through the following response chain."""

    turns: list[tuple[BaseMessage, ...]] = []
    current_turn: list[BaseMessage] = []
    for message in history:
        if isinstance(message, HumanMessage):
            if current_turn:
                turns.append(tuple(current_turn))
            current_turn = [message]
        elif current_turn:
            current_turn.append(message)
    if current_turn:
        turns.append(tuple(current_turn))
    return tuple(turns)


def _estimate_text_tokens(text: str) -> int:
    if not isinstance(text, str):
        raise TypeError("token estimation requires text content")
    ascii_characters = sum(character.isascii() for character in text)
    non_ascii_characters = len(text) - ascii_characters
    return max(1, (ascii_characters + 3) // 4 + (non_ascii_characters + 1) // 2)


def _validate_messages(messages: Sequence[BaseMessage]) -> None:
    if not isinstance(messages, Sequence) or not all(
        isinstance(message, BaseMessage) for message in messages
    ):
        raise TypeError("messages must be a sequence of BaseMessage instances")
