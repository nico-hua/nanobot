"""Build the system prompt from the current workspace context files."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path

from ..providers import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

logger = logging.getLogger(__name__)

_BASE_PROMPT = "You are Nanobot, a helpful AI assistant."
DEFAULT_HISTORY_TOKEN_BUDGET = 64_000
_MESSAGE_OVERHEAD_TOKENS = 4
_CONTEXT_FILES = (
    ("AGENTS.md", "## Workspace Instructions"),
    ("SOUL.md", "## Agent Style"),
    ("USER.md", "## User Profile"),
)


class ContextBuilder:
    """Build a fresh system prompt using optional workspace Markdown files."""

    def __init__(
        self,
        workspace: str | Path,
        history_token_budget: int = DEFAULT_HISTORY_TOKEN_BUDGET,
    ) -> None:
        if not isinstance(workspace, (str, Path)):
            raise TypeError("ContextBuilder workspace must be a string or Path")
        if not isinstance(history_token_budget, int) or isinstance(
            history_token_budget,
            bool,
        ):
            raise TypeError("ContextBuilder history_token_budget must be an integer")
        if history_token_budget < 0:
            raise ValueError("ContextBuilder history_token_budget must not be negative")
        self._workspace = Path(workspace).resolve()
        self._history_token_budget = history_token_budget

    def build_system_prompt(self) -> str:
        """Return a prompt built from the current workspace file contents."""

        sections = [
            "\n".join(
                (
                    "# Nanobot",
                    "",
                    _BASE_PROMPT,
                    "",
                    "## Workspace",
                    "",
                    f"`{self._workspace}`",
                )
            )
        ]
        for filename, title in _CONTEXT_FILES:
            content = self._read_optional_file(filename)
            if content is not None:
                sections.append(f"{title}\n\n{content}")
        return "\n\n".join(sections)

    def build_request_messages(
        self,
        history: Sequence[BaseMessage],
        current_message: HumanMessage,
    ) -> tuple[BaseMessage, ...]:
        """Build one request with a fresh system prompt and trimmed history."""

        _validate_messages(history)
        if not isinstance(current_message, HumanMessage):
            raise TypeError("ContextBuilder current_message must be a HumanMessage")

        normalized_history = tuple(
            message for message in history if not isinstance(message, SystemMessage)
        )
        if normalized_history and normalized_history[-1] == current_message:
            normalized_history = normalized_history[:-1]
        return (
            SystemMessage(content=self.build_system_prompt()),
            *self.trim_history(normalized_history),
            current_message,
        )

    def trim_history(self, history: Sequence[BaseMessage]) -> tuple[BaseMessage, ...]:
        """Keep the newest complete user turns that fit the history budget."""

        _validate_messages(history)
        turns = _split_user_turns(
            tuple(message for message in history if not isinstance(message, SystemMessage))
        )
        selected_turns: list[tuple[BaseMessage, ...]] = []
        remaining_tokens = self._history_token_budget
        for turn in reversed(turns):
            turn_tokens = estimate_messages_tokens(turn)
            if turn_tokens > remaining_tokens:
                break
            selected_turns.append(turn)
            remaining_tokens -= turn_tokens
        return tuple(
            message
            for turn in reversed(selected_turns)
            for message in turn
        )

    def _read_optional_file(self, filename: str) -> str | None:
        try:
            content = (self._workspace / filename).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError):
            logger.warning("Skipping unreadable workspace context file (name=%s)", filename)
            return None
        return content if content.strip() else None


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


def _split_user_turns(
    history: tuple[BaseMessage, ...],
) -> tuple[tuple[BaseMessage, ...], ...]:
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
