"""Single-turn orchestration around the minimal agent runner."""

from __future__ import annotations

import asyncio
import logging

from ..bus import MessageBus, OutboundMessage
from ..providers import HumanMessage, LLMProvider, SystemMessage
from ..session import SessionManager
from ..tools import ToolRegistry
from .runner import AgentRunner, AgentRunResult, AgentRunSpec

logger = logging.getLogger(__name__)

_DEFAULT_SYSTEM_PROMPT = "你是一个有用的助手"


class AgentLoop:
    """Run one message against a persistent session and its system prompt."""

    def __init__(
        self,
        runner: AgentRunner,
        provider: LLMProvider,
        tool_registry: ToolRegistry,
        session_manager: SessionManager,
        message_bus: MessageBus | None = None,
    ) -> None:
        if not isinstance(runner, AgentRunner):
            raise TypeError("AgentLoop requires an AgentRunner")
        if not isinstance(provider, LLMProvider):
            raise TypeError("AgentLoop requires an LLMProvider")
        if not isinstance(tool_registry, ToolRegistry):
            raise TypeError("AgentLoop requires a ToolRegistry")
        if not isinstance(session_manager, SessionManager):
            raise TypeError("AgentLoop requires a SessionManager")
        if message_bus is not None and not isinstance(message_bus, MessageBus):
            raise TypeError("AgentLoop message_bus must be a MessageBus")

        self._runner = runner
        self._provider = provider
        self._tool_registry = tool_registry
        self._session_manager = session_manager
        self._message_bus = message_bus

    async def run(self) -> None:
        """Continuously process inbound bus messages until cancelled."""

        if self._message_bus is None:
            raise RuntimeError("AgentLoop requires a MessageBus for continuous running")

        logger.info("Agent loop started")
        try:
            while True:
                try:
                    inbound = await asyncio.wait_for(
                        self._message_bus.consume_inbound(),
                        timeout=1,
                    )
                except TimeoutError:
                    continue
                try:
                    result = await self.process_direct(
                        inbound.content,
                        inbound.channel,
                        inbound.chat_id,
                        inbound.session_id,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Agent loop failed while processing an inbound message")
                    raise
                await self._message_bus.publish_outbound(
                    OutboundMessage(
                        channel=inbound.channel,
                        chat_id=inbound.chat_id,
                        sender_id=inbound.sender_id,
                        session_id=inbound.session_id,
                        content=result.content or "",
                    )
                )
        except asyncio.CancelledError:
            logger.info("Agent loop cancelled")
            raise

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
        return await self._run_once(content, _session_key(channel, chat_id, session_id))

    async def _run_once(self, content: str, session_key: str) -> AgentRunResult:
        """Persist one user message, run it, then persist the completed history."""

        session = self._session_manager.get_or_create(session_key)
        history = session.messages
        if not any(isinstance(message, SystemMessage) for message in history):
            history = (SystemMessage(content=_DEFAULT_SYSTEM_PROMPT), *history)
        session = self._session_manager.save(
            session.with_messages((*history, HumanMessage(content=content)))
        )
        spec = AgentRunSpec(
            messages=session.messages,
            provider=self._provider,
            tool_registry=self._tool_registry,
        )
        result = await self._runner.run(spec)
        self._session_manager.save(session.with_messages(result.messages))
        return result


def _validate_direct_routing(channel: str, chat_id: str, session_id: str) -> None:
    for name, value in (
        ("channel", channel),
        ("chat_id", chat_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
    if not isinstance(session_id, str):
        raise TypeError("session_id must be a string")


def _session_key(channel: str, chat_id: str, session_id: str) -> str:
    return session_id if session_id.strip() else f"{channel}:{chat_id}"
