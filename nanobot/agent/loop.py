"""Single-turn orchestration around the minimal agent runner."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from ..bus import MessageBus, OutboundMessage
from ..memory import MemoryConsolidator
from ..providers import BaseMessage, HumanMessage, LLMProvider, SystemMessage
from ..session import SessionCompactor, SessionManager
from ..tools import ToolRegistry
from .context import ContextBuilder, ContextWindowExceededError
from .runner import AgentRunner, AgentRunResult, AgentRunSpec

logger = logging.getLogger(__name__)

_CONTEXT_WINDOW_EXCEEDED_MESSAGE = (
    "当前会话的系统提示词、摘要或当前消息已超过模型上下文窗口，"
    "请新开会话或缩短消息后重试。"
)


class AgentLoop:
    """Run one message against a persistent session and fresh system prompt."""

    def __init__(
        self,
        runner: AgentRunner,
        provider: LLMProvider,
        tool_registry: ToolRegistry,
        session_manager: SessionManager,
        context_builder: ContextBuilder,
        message_bus: MessageBus | None = None,
        session_compactor: SessionCompactor | None = None,
        memory_consolidator: MemoryConsolidator | None = None,
    ) -> None:
        if not isinstance(runner, AgentRunner):
            raise TypeError("AgentLoop requires an AgentRunner")
        if not isinstance(provider, LLMProvider):
            raise TypeError("AgentLoop requires an LLMProvider")
        if not isinstance(tool_registry, ToolRegistry):
            raise TypeError("AgentLoop requires a ToolRegistry")
        if not isinstance(session_manager, SessionManager):
            raise TypeError("AgentLoop requires a SessionManager")
        if not isinstance(context_builder, ContextBuilder):
            raise TypeError("AgentLoop requires a ContextBuilder")
        if message_bus is not None and not isinstance(message_bus, MessageBus):
            raise TypeError("AgentLoop message_bus must be a MessageBus")
        if session_compactor is not None and not isinstance(
            session_compactor,
            SessionCompactor,
        ):
            raise TypeError("AgentLoop session_compactor must be a SessionCompactor")
        if memory_consolidator is not None and not isinstance(
            memory_consolidator,
            MemoryConsolidator,
        ):
            raise TypeError("AgentLoop memory_consolidator must be a MemoryConsolidator")

        self._runner = runner
        self._provider = provider
        self._tool_registry = tool_registry
        self._session_manager = session_manager
        self._context_builder = context_builder
        self._message_bus = message_bus
        self._session_compactor = session_compactor
        self._memory_consolidator = memory_consolidator
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._compaction_tasks: set[asyncio.Task[None]] = set()
        self._memory_consolidation_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

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
                        inbound.metadata,
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
            await self.close()
            raise

    async def close(self) -> None:
        """Cancel and await all tracked background session tasks."""

        if self._closed:
            return
        self._closed = True
        tasks = tuple(self._compaction_tasks | self._memory_consolidation_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def wait_for_compactions(self) -> None:
        """Wait until currently scheduled background compactions have completed."""

        while self._compaction_tasks:
            tasks = tuple(self._compaction_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
            self._compaction_tasks.difference_update(
                task for task in tasks if task.done()
            )

    async def wait_for_memory_consolidations(self) -> None:
        """Wait until currently scheduled memory consolidations have completed."""

        while self._memory_consolidation_tasks:
            tasks = tuple(self._memory_consolidation_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
            self._memory_consolidation_tasks.difference_update(
                task for task in tasks if task.done()
            )

    async def process_direct(
        self,
        content: str,
        channel: str,
        chat_id: str,
        session_id: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> AgentRunResult:
        """Process one routed message without reading or writing bus queues."""

        if not isinstance(content, str):
            raise TypeError("content must be a string")
        _validate_direct_routing(channel, chat_id, session_id)
        _validate_metadata(metadata)
        return await self._run_once(
            content,
            _session_key(channel, chat_id, session_id),
            consolidate_memory=_is_memory_eligible(content, metadata),
        )

    async def _run_once(
        self,
        content: str,
        session_key: str,
        *,
        consolidate_memory: bool,
    ) -> AgentRunResult:
        """Persist one user message, run it, then persist the completed history."""

        async with self._lock_for(session_key):
            session = self._session_manager.get_or_create(session_key)
            history = _without_system_messages(session.messages)
            current_message = HumanMessage(content=content)
            try:
                request_messages = self._context_builder.build_request_messages(
                    history,
                    current_message,
                    summary=session.summary,
                    summary_until=session.summary_until,
                    tools=self._tool_registry.tools,
                )
            except ContextWindowExceededError:
                logger.warning("Agent request exceeds the configured context window")
                return AgentRunResult(
                    content=_CONTEXT_WINDOW_EXCEEDED_MESSAGE,
                    messages=(),
                    tools_used=(),
                    token_usage=None,
                    stop_reason="context_window_exceeded",
                )

            session = self._session_manager.save(
                session.with_messages((*history, current_message))
            )
            spec = AgentRunSpec(
                messages=request_messages,
                provider=self._provider,
                tool_registry=self._tool_registry,
            )
            result = await self._runner.run(spec)
            completed_messages = _without_system_messages(result.messages[len(spec.messages) :])
            completed_session = session.with_messages(
                (
                    *session.messages,
                    *completed_messages,
                )
            )
            self._session_manager.save(completed_session)
            memory_snapshot = (current_message, *completed_messages)
            if consolidate_memory:
                self._schedule_memory_consolidation(memory_snapshot)
            self._schedule_compaction(session_key)
            return result

    def _schedule_compaction(self, session_key: str) -> None:
        if self._session_compactor is None or self._closed:
            return
        task = asyncio.create_task(self._compact_session(session_key))
        self._compaction_tasks.add(task)
        task.add_done_callback(self._compaction_tasks.discard)

    async def _compact_session(self, session_key: str) -> None:
        try:
            async with self._lock_for(session_key):
                session = self._session_manager.get_or_create(session_key)
                compacted_session = await self._session_compactor.compact(session)
                if compacted_session is not session:
                    self._session_manager.save(compacted_session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background session compaction failed")

    def _schedule_memory_consolidation(
        self,
        messages: tuple[BaseMessage, ...],
    ) -> None:
        if self._memory_consolidator is None or self._closed:
            return
        task = asyncio.create_task(self._consolidate_memory(messages))
        self._memory_consolidation_tasks.add(task)
        task.add_done_callback(self._memory_consolidation_tasks.discard)

    async def _consolidate_memory(self, messages: tuple[BaseMessage, ...]) -> None:
        try:
            await self._memory_consolidator.consolidate(messages)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background long-term memory consolidation failed")

    def _lock_for(self, session_key: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_key] = lock
        return lock


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


def _without_system_messages(messages: tuple[BaseMessage, ...]) -> tuple[BaseMessage, ...]:
    return tuple(message for message in messages if not isinstance(message, SystemMessage))


def _validate_metadata(metadata: Mapping[str, Any] | None) -> None:
    if metadata is not None and not isinstance(metadata, Mapping):
        raise TypeError("metadata must be a mapping or None")


def _is_memory_eligible(content: str, metadata: Mapping[str, Any] | None) -> bool:
    if content.lstrip().startswith("/"):
        return False
    if metadata is None:
        return True
    if metadata.get("ephemeral") is True or metadata.get("is_ephemeral") is True:
        return False
    if metadata.get("message_type") == "system" or metadata.get("role") == "system":
        return False
    return metadata.get("source") != "memory_consolidator"
