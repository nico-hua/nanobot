"""Single-turn orchestration around the minimal agent runner."""

from __future__ import annotations

from collections.abc import Sequence

from ..providers import BaseMessage, HumanMessage, LLMProvider
from ..tools import ToolRegistry
from .runner import AgentRunner, AgentRunResult, AgentRunSpec


class SessionStore:
    """In-memory message histories keyed by session ID."""

    def __init__(self) -> None:
        self._messages_by_session: dict[str, tuple[BaseMessage, ...]] = {}

    def load(self, session_id: str) -> tuple[BaseMessage, ...]:
        """Return one session's messages, or an empty history."""

        _validate_session_id(session_id)
        return self._messages_by_session.get(session_id, ())

    def save(self, session_id: str, messages: Sequence[BaseMessage]) -> None:
        """Replace one session's complete message history."""

        _validate_session_id(session_id)
        if not isinstance(messages, Sequence) or not all(
            isinstance(message, BaseMessage) for message in messages
        ):
            raise TypeError("messages must be a sequence of BaseMessage instances")
        self._messages_by_session[session_id] = tuple(messages)


class AgentLoop:
    """Load one session, execute one agent run, then save its full history."""

    def __init__(
        self,
        runner: AgentRunner,
        provider: LLMProvider,
        tool_registry: ToolRegistry,
        session_store: SessionStore | None = None,
    ) -> None:
        if not isinstance(runner, AgentRunner):
            raise TypeError("AgentLoop requires an AgentRunner")
        if not isinstance(provider, LLMProvider):
            raise TypeError("AgentLoop requires an LLMProvider")
        if not isinstance(tool_registry, ToolRegistry):
            raise TypeError("AgentLoop requires a ToolRegistry")
        if session_store is not None and not isinstance(session_store, SessionStore):
            raise TypeError("AgentLoop session_store must be a SessionStore")

        self._runner = runner
        self._provider = provider
        self._tool_registry = tool_registry
        self._session_store = session_store if session_store is not None else SessionStore()

    async def run(self, user_message: str, session_id: str) -> AgentRunResult:
        """Run one user message against a session's existing history."""

        if not isinstance(user_message, str):
            raise TypeError("user_message must be a string")

        history = self._session_store.load(session_id)
        spec = AgentRunSpec(
            messages=(*history, HumanMessage(content=user_message)),
            provider=self._provider,
            tool_registry=self._tool_registry,
        )
        result = await self._runner.run(spec)
        self._session_store.save(session_id, result.messages)
        return result


def _validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id must be a non-empty string")
