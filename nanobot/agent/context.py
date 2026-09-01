"""Build the system prompt from the current workspace context files."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path

from ..memory import MemoryStore
from ..providers import BaseMessage, HumanMessage, SystemMessage
from ..session.tokens import (
    estimate_message_tokens,  # noqa: F401
    estimate_messages_tokens,
    split_user_turns,
)
from ..skills import SkillLoadError, SkillNotFoundError, SkillsLoader
from ..tools import Tool

logger = logging.getLogger(__name__)

_BASE_PROMPT = "You are Nanobot, a helpful AI assistant."
DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000
DEFAULT_OUTPUT_TOKEN_RESERVE = 1_024
_MESSAGE_OVERHEAD_TOKENS = 4
_CONTEXT_FILES = (
    ("AGENTS.md", "## Workspace Instructions"),
    ("SOUL.md", "## Agent Style"),
    ("USER.md", "## User Profile"),
)


class ContextWindowExceededError(RuntimeError):
    """Raised when required request content alone exceeds the model context window."""


class ContextBuilder:
    """Build a fresh system prompt using workspace files, memory, and Skills."""

    def __init__(
        self,
        workspace: str | Path,
        context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS,
        output_token_reserve: int = DEFAULT_OUTPUT_TOKEN_RESERVE,
    ) -> None:
        if not isinstance(workspace, (str, Path)):
            raise TypeError("ContextBuilder workspace must be a string or Path")
        _validate_token_budget("context_window_tokens", context_window_tokens, positive=True)
        _validate_token_budget("output_token_reserve", output_token_reserve, positive=False)
        self._workspace = Path(workspace).resolve()
        self._context_window_tokens = context_window_tokens
        self._output_token_reserve = output_token_reserve
        self._memory_store = MemoryStore(self._workspace)
        self._skills_loader = SkillsLoader(self._workspace)

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
        memory = self._memory_store.read()
        if memory:
            sections.append(f"## Long-term Memory\n\n{memory}")
        sections.extend(self._build_skill_sections())
        return "\n\n".join(sections)

    def build_request_messages(
        self,
        history: Sequence[BaseMessage],
        current_message: HumanMessage,
        *,
        summary: str | None = None,
        summary_until: int = 0,
        tools: Sequence[Tool] = (),
    ) -> tuple[BaseMessage, ...]:
        """Build one request with a fresh prompt, optional summary, and trimmed history."""

        _validate_messages(history)
        if not isinstance(current_message, HumanMessage):
            raise TypeError("ContextBuilder current_message must be a HumanMessage")
        if summary is not None and (not isinstance(summary, str) or not summary.strip()):
            raise ValueError("ContextBuilder summary must be a non-empty string or None")
        if not isinstance(summary_until, int) or isinstance(summary_until, bool):
            raise TypeError("ContextBuilder summary_until must be an integer")
        if summary_until < 0:
            raise ValueError("ContextBuilder summary_until must not be negative")
        _validate_tools(tools)

        normalized_history = tuple(
            message for message in history if not isinstance(message, SystemMessage)
        )
        if normalized_history and normalized_history[-1] == current_message:
            normalized_history = normalized_history[:-1]
        if summary_until > len(normalized_history):
            raise ValueError("ContextBuilder summary_until exceeds the history length")
        system_prompt = self.build_system_prompt()
        if summary is not None:
            system_prompt = f"{system_prompt}\n\n## Conversation Summary\n\n{summary}"
        active_skills = self._build_active_skill_context(current_message.content)
        if active_skills is not None:
            system_prompt = f"{system_prompt}\n\n{active_skills}"
        system_message = SystemMessage(content=system_prompt)
        available_history_budget = self._available_history_budget(
            (system_message, current_message),
            tools,
        )
        return (
            system_message,
            *self.trim_history(
                normalized_history[summary_until:],
                token_budget=available_history_budget,
            ),
            current_message,
        )

    def trim_history(
        self,
        history: Sequence[BaseMessage],
        *,
        token_budget: int,
    ) -> tuple[BaseMessage, ...]:
        """Keep the newest complete user turns that fit the history budget."""

        _validate_messages(history)
        _validate_token_budget("token_budget", token_budget, positive=False)
        turns = split_user_turns(
            tuple(message for message in history if not isinstance(message, SystemMessage))
        )
        selected_turns: list[tuple[BaseMessage, ...]] = []
        remaining_tokens = token_budget
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

    def _available_history_budget(
        self,
        required_messages: Sequence[BaseMessage],
        tools: Sequence[Tool],
    ) -> int:
        required_tokens = (
            estimate_messages_tokens(required_messages)
            + estimate_tools_tokens(tools)
            + self._output_token_reserve
        )
        available_history_budget = self._context_window_tokens - required_tokens
        if available_history_budget < 0:
            raise ContextWindowExceededError(
                "Required context exceeds the configured model context window"
            )
        return available_history_budget

    def _read_optional_file(self, filename: str) -> str | None:
        try:
            content = (self._workspace / filename).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError):
            logger.warning("Skipping unreadable workspace context file (name=%s)", filename)
            return None
        return content if content.strip() else None

    def _build_skill_sections(self) -> tuple[str, ...]:
        """Build static Skill context without executing or persisting Skill contents."""

        try:
            skills = self._skills_loader.list_skills()
        except Exception:
            logger.exception("Skipping Skills after discovery failed")
            return ()

        sections: list[str] = []
        always_active = []
        for skill in skills:
            if not skill.always or not skill.is_available:
                continue
            try:
                content = self._skills_loader.read_skill(skill.name)
            except (SkillLoadError, SkillNotFoundError):
                logger.warning("Skipping unavailable always-active Skill (name=%s)", skill.name)
                continue
            except Exception:
                logger.exception("Skipping always-active Skill after loading failed (name=%s)", skill.name)
                continue
            if content.strip():
                always_active.append(f"### {skill.name}\n\n{content}")
        if always_active:
            sections.append("## Always-active Skills\n\n" + "\n\n".join(always_active))

        available_skills = [
            skill for skill in skills if not skill.always and skill.is_available
        ]
        if available_skills:
            entries = [
                "- "
                f"**{skill.name}**: {skill.description or 'No description provided.'} "
                f"(`{skill.path}`)"
                for skill in available_skills
            ]
            sections.append("## Available Skills\n\n" + "\n".join(entries))
        unavailable_skills = [skill for skill in skills if not skill.is_available]
        if unavailable_skills:
            entries = [
                "- "
                f"**{skill.name}**: {skill.description or 'No description provided.'} "
                f"Unavailable; missing {', '.join(f'`{dependency}`' for dependency in skill.missing_dependencies)}. "
                f"(`{skill.path}`)"
                for skill in unavailable_skills
            ]
            sections.append("## Unavailable Skills\n\n" + "\n".join(entries))
        return tuple(sections)

    def _build_active_skill_context(self, content: str) -> str | None:
        """Load explicitly referenced non-always Skills for this request only."""

        try:
            referenced_skills = self._skills_loader.find_referenced_skills(content)
        except Exception:
            logger.exception("Skipping explicit Skills after reference discovery failed")
            return None

        active_skills = []
        unavailable_skills = []
        for skill in referenced_skills:
            if not skill.is_available:
                unavailable_skills.append(
                    f"- **{skill.name}** is unavailable: missing "
                    + ", ".join(
                        f"`{dependency}`" for dependency in skill.missing_dependencies
                    )
                    + "."
                )
                continue
            if skill.always:
                continue
            try:
                skill_content = self._skills_loader.read_skill(skill.name)
            except (SkillLoadError, SkillNotFoundError):
                logger.warning("Skipping unavailable explicit Skill (name=%s)", skill.name)
                continue
            except Exception:
                logger.exception("Skipping explicit Skill after loading failed (name=%s)", skill.name)
                continue
            if skill_content.strip():
                active_skills.append(f"### {skill.name}\n\n{skill_content}")
        sections: list[str] = []
        if active_skills:
            sections.append(
                "[Active Skills for this turn]\n\n"
                + "\n\n".join(active_skills)
                + "\n\n[/Active Skills]"
            )
        if unavailable_skills:
            sections.append(
                "[Unavailable Skills requested for this turn]\n\n"
                + "\n".join(unavailable_skills)
                + "\n\n[/Unavailable Skills]"
            )
        if not sections:
            return None
        return "\n\n".join(sections)


def estimate_tools_tokens(tools: Sequence[Tool]) -> int:
    """Return a stable approximate token count for registered tool definitions."""

    _validate_tools(tools)
    schemas = [
        {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters_schema,
        }
        for tool in tools
    ]
    if not schemas:
        return 0
    return _MESSAGE_OVERHEAD_TOKENS * len(schemas) + _estimate_text_tokens(
        json.dumps(
            schemas,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
    )


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


def _validate_tools(tools: Sequence[Tool]) -> None:
    if not isinstance(tools, Sequence) or not all(isinstance(tool, Tool) for tool in tools):
        raise TypeError("tools must be a sequence of Tool instances")


def _validate_token_budget(name: str, value: int, *, positive: bool) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if value < 0 or (positive and value == 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be {qualifier}")
