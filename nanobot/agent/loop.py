"""Single-turn orchestration around the minimal agent runner."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from ..bus import MessageBus, OutboundMessage
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
        message_bus: MessageBus | None = None,
    ) -> None:
        if not isinstance(runner, AgentRunner):
            raise TypeError("AgentLoop requires an AgentRunner")
        if not isinstance(provider, LLMProvider):
            raise TypeError("AgentLoop requires an LLMProvider")
        if not isinstance(tool_registry, ToolRegistry):
            raise TypeError("AgentLoop requires a ToolRegistry")
        if session_store is not None and not isinstance(session_store, SessionStore):
            raise TypeError("AgentLoop session_store must be a SessionStore")
        if message_bus is not None and not isinstance(message_bus, MessageBus):
            raise TypeError("AgentLoop message_bus must be a MessageBus")

        self._runner = runner
        self._provider = provider
        self._tool_registry = tool_registry
        self._session_store = session_store if session_store is not None else SessionStore()
        self._message_bus = message_bus

    async def run(self) -> None:
        """Continuously process inbound bus messages until cancelled."""

        if self._message_bus is None:
            raise RuntimeError("AgentLoop requires a MessageBus for continuous running")

        while True:
            try:
                inbound = await asyncio.wait_for(
                    self._message_bus.consume_inbound(),
                    timeout=1,
                )
            except TimeoutError:
                continue
            result = await self.process_direct(
                inbound.content,
                inbound.channel,
                inbound.chat_id,
                inbound.session_id,
            )
            await self._message_bus.publish_outbound(
                OutboundMessage(
                    channel=inbound.channel,
                    chat_id=inbound.chat_id,
                    sender_id=inbound.sender_id,
                    session_id=inbound.session_id,
                    content=result.content or "",
                )
            )

    async def process_direct(
        self,
        content: str,
        channel: str,
        chat_id: str,
        session_id: str,
    ) -> AgentRunResult:
        """Process one routed message without reading or writing bus queues."""

        if not isinstance(content, str):
            raise TypeError("content must be a string")
        _validate_direct_routing(channel, chat_id, session_id)
        return await self._run_once(content, session_id)

    async def _run_once(self, content: str, session_id: str) -> AgentRunResult:
        """Run one validated user message against a session's existing history."""

        history = self._session_store.load(session_id)
        spec = AgentRunSpec(
            messages=(*history, HumanMessage(content=content)),
            provider=self._provider,
            tool_registry=self._tool_registry,
        )
        result = await self._runner.run(spec)
        self._session_store.save(session_id, result.messages)
        return result


def _validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id must be a non-empty string")


def _validate_direct_routing(channel: str, chat_id: str, session_id: str) -> None:
    for name, value in (
        ("channel", channel),
        ("chat_id", chat_id),
        ("session_id", session_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
