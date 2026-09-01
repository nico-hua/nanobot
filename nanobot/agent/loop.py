"""Single-turn orchestration around the minimal agent runner."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from ..bus import InboundMessage, MessageBus, OutboundMessage
from ..memory import MemoryConsolidator, MemoryEventConsumer, MemoryStore
from ..providers import BaseMessage, HumanMessage, LLMProvider, SystemMessage
from ..session import SessionCompactor, SessionManager
from ..tools import ToolRegistry
from .commands import CommandInvocation, CommandRouter
from .context import ContextBuilder, ContextWindowExceededError
from .runner import AgentRunner, AgentRunSpec

logger = logging.getLogger(__name__)

_CONTEXT_WINDOW_EXCEEDED_MESSAGE = (
    "当前会话的系统提示词、摘要或当前消息已超过模型上下文窗口，"
    "请新开会话或缩短消息后重试。"
)
_PROCESSING_ERROR_MESSAGE = "处理消息时发生错误，请稍后重试。"


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
        memory_store: MemoryStore | None = None,
        memory_consolidator: MemoryConsolidator | None = None,
        command_router: CommandRouter | None = None,
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
        if memory_store is not None and not isinstance(memory_store, MemoryStore):
            raise TypeError("AgentLoop memory_store must be a MemoryStore")
        if (memory_store is None) != (memory_consolidator is None):
            raise ValueError(
                "AgentLoop memory_store and memory_consolidator must be configured together"
            )
        if command_router is not None and not isinstance(command_router, CommandRouter):
            raise TypeError("AgentLoop command_router must be a CommandRouter")

        self._runner = runner
        self._provider = provider
        self._tool_registry = tool_registry
        self._session_manager = session_manager
        self._context_builder = context_builder
        self._message_bus = message_bus
        self._session_compactor = session_compactor
        self._memory_events = (
            MemoryEventConsumer(memory_store, memory_consolidator)
            if memory_store is not None and memory_consolidator is not None
            else None
        )
        self._command_router = command_router or CommandRouter(
            session_manager,
            session_compactor=session_compactor,
            memory_store=memory_store,
            cancel_active_turn=self._cancel_active_turn,
        )
        # 同一 session 的写入、压缩和命令操作必须按顺序执行。
        self._session_locks: dict[str, asyncio.Lock] = {}
        # 普通 turn 在真正开始前就登记，保证紧随其后的 /stop 也能取消它。
        self._active_turn_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._inbound_tasks: set[asyncio.Task[None]] = set()
        self._compaction_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    async def run(self) -> None:
        """Continuously process inbound bus messages until cancelled."""

        if self._message_bus is None:
            raise RuntimeError("AgentLoop requires a MessageBus for continuous running")

        logger.info("Agent loop started")
        # 重启后先尝试消费 cursor 之后遗留的长期记忆事件。
        if self._memory_events is not None:
            self._memory_events.wake()
        try:
            while True:
                try:
                    inbound = await asyncio.wait_for(
                        self._message_bus.consume_inbound(),
                        timeout=1,
                    )
                except TimeoutError:
                    continue
                invocation = self._command_router.parse(inbound.content)
                if self._command_router.is_stop_command(invocation):
                    # /stop 不能等待当前 session 的锁，否则无法停止正在运行的 turn。
                    await self._handle_stop(inbound, invocation)
                else:
                    # MessageBus 已是入站队列；非 /stop 消息由统一处理器异步消费。
                    self._enqueue_message(inbound, invocation)
        except asyncio.CancelledError:
            logger.info("Agent loop cancelled")
            await self.close()
            raise

    async def close(self) -> None:
        """Cancel and await all tracked background session tasks."""

        if self._closed:
            return
        self._closed = True
        # Application 关闭时统一取消请求、压缩和记忆消费者，避免遗留后台任务。
        tasks = set(self._compaction_tasks)
        tasks.update(self._inbound_tasks)
        for turn_tasks in self._active_turn_tasks.values():
            tasks.update(turn_tasks)
        current_task = asyncio.current_task()
        tasks.discard(current_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._memory_events is not None:
            await self._memory_events.close()
        self._inbound_tasks.clear()
        self._active_turn_tasks.clear()

    async def wait_for_compactions(self) -> None:
        """Wait until currently scheduled background compactions have completed."""

        while self._compaction_tasks:
            tasks = tuple(self._compaction_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
            self._compaction_tasks.difference_update(
                task for task in tasks if task.done()
            )

    async def wait_for_memory_consolidations(self) -> None:
        """Wait until the currently scheduled memory event consumer finishes."""

        if self._memory_events is not None:
            await self._memory_events.wait()

    # -- MessageBus queue -------------------------------------------------

    async def _handle_stop(
        self,
        inbound: InboundMessage,
        invocation: CommandInvocation,
    ) -> None:
        """Cancel the current session turn and publish only the stop command reply."""

        try:
            response = await self._run_stop_command(inbound, invocation)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Agent loop failed while handling /stop")
            response = _error_outbound_message(inbound)
        await self._message_bus.publish_outbound(response)

    def _enqueue_message(
        self,
        inbound: InboundMessage,
        invocation: CommandInvocation | None,
    ) -> None:
        """Schedule one non-stop queue item while making normal turns stoppable."""

        task = asyncio.create_task(self._process_queued_message(inbound, invocation))
        self._inbound_tasks.add(task)
        task.add_done_callback(self._inbound_tasks.discard)
        if invocation is None:
            # 在 task 获得运行机会前先登记，处理“普通消息后立刻收到 /stop”的竞态。
            self._track_turn_task(
                _session_key(inbound.channel, inbound.chat_id, inbound.session_id),
                task,
            )

    async def _process_queued_message(
        self,
        inbound: InboundMessage,
        invocation: CommandInvocation | None,
    ) -> None:
        """Process one queued item and publish either a command or Agent response."""

        try:
            response = await self._dispatch_non_stop_message(inbound, invocation)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 单条消息失败不应终止持续运行的总线消费循环。
            logger.exception("Agent loop failed while processing an inbound message")
            response = _error_outbound_message(inbound)
        await self._message_bus.publish_outbound(response)

    # -- Message dispatch -------------------------------------------------

    async def _run_stop_command(
        self,
        inbound: InboundMessage,
        invocation: CommandInvocation,
    ) -> OutboundMessage:
        """Run the one command that must not wait for the session lock."""

        session_key = _session_key(
            inbound.channel,
            inbound.chat_id,
            inbound.session_id,
        )
        return _require_command_reply(
            await self._command_router.route(
                inbound,
                session_key,
                None,
                invocation,
            )
        )

    async def _dispatch_non_stop_message(
        self,
        inbound: InboundMessage,
        invocation: CommandInvocation | None,
    ) -> OutboundMessage:
        """Route a non-stop message to CommandRouter or AgentRunner."""

        session_key = _session_key(
            inbound.channel,
            inbound.chat_id,
            inbound.session_id,
        )
        if invocation is None:
            return await self._run_turn(inbound, session_key)

        # 队列中的命令都不是 /stop；它们会读取或修改 Session，必须与普通 turn 串行化。
        async with self._lock_for(session_key):
            session = self._session_manager.get_or_create(session_key)
            return _require_command_reply(
                await self._command_router.route(
                    inbound,
                    session_key,
                    session,
                    invocation,
                )
            )

    # -- Normal Agent turn ------------------------------------------------

    async def _run_turn(
        self,
        inbound: InboundMessage,
        session_key: str,
    ) -> OutboundMessage:
        """Persist one user message, run it, then persist the completed history."""

        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("AgentLoop requires a running asyncio task")
        self._track_turn_task(session_key, task)
        try:
            # 锁覆盖“读取历史 -> 请求模型 -> 保存结果”，防止同一会话交错写入。
            async with self._lock_for(session_key):
                session = self._session_manager.get_or_create(session_key)
                history = _without_system_messages(session.messages)
                current_message = HumanMessage(content=inbound.content)
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
                    return _outbound_message(
                        inbound,
                        _CONTEXT_WINDOW_EXCEEDED_MESSAGE,
                    )

                # 先保存本轮用户消息；模型调用失败时也不会丢失已开始的对话。
                session = self._session_manager.save(
                    session.with_messages((*history, current_message))
                )
                spec = AgentRunSpec(
                    messages=request_messages,
                    provider=self._provider,
                    tool_registry=self._tool_registry,
                )
                result = await self._runner.run(spec)
                completed_messages = _without_system_messages(
                    result.messages[len(spec.messages) :]
                )
                completed_session = session.with_messages(
                    (
                        *session.messages,
                        *completed_messages,
                    )
                )
                self._session_manager.save(completed_session)
                # 事件只包含已成功保存的本轮快照，绝不直接引用可继续变化的 Session。
                memory_snapshot = (current_message, *completed_messages)
                if _is_memory_eligible(inbound.content, inbound.metadata) and self._memory_events is not None:
                    self._memory_events.append(session_key, memory_snapshot)
                # 摘要在后台执行，不能延迟当前用户回复。
                self._schedule_compaction(session_key)
                return _outbound_message(inbound, result.content or "")
        finally:
            self._untrack_turn_task(session_key, task)

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

    def _lock_for(self, session_key: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_key] = lock
        return lock

    def _track_turn_task(self, session_key: str, task: asyncio.Task[Any]) -> None:
        tasks = self._active_turn_tasks.setdefault(session_key, set())
        if task in tasks:
            return
        tasks.add(task)
        task.add_done_callback(
            lambda completed_task: self._untrack_turn_task(session_key, completed_task)
        )

    def _untrack_turn_task(self, session_key: str, task: asyncio.Task[Any]) -> None:
        tasks = self._active_turn_tasks.get(session_key)
        if tasks is None:
            return
        tasks.discard(task)
        if not tasks:
            self._active_turn_tasks.pop(session_key, None)

    def _cancel_active_turn(self, session_key: str) -> bool:
        current_task = asyncio.current_task()
        tasks = tuple(self._active_turn_tasks.get(session_key, ()))
        # /stop 只影响当前会话，不会取消其他 channel 或 chat 的请求。
        active_tasks = tuple(
            task
            for task in tasks
            if not task.done() and task is not current_task
        )
        for task in active_tasks:
            task.cancel()
        if active_tasks:
            logger.info("Cancelling active agent turns (count=%d)", len(active_tasks))
        return bool(active_tasks)


def _session_key(channel: str, chat_id: str, session_id: str) -> str:
    return session_id if session_id.strip() else f"{channel}:{chat_id}"


def _require_command_reply(reply: OutboundMessage | None) -> OutboundMessage:
    if reply is None:
        raise RuntimeError("CommandRouter did not return a command reply")
    return reply


def _outbound_message(
    inbound: InboundMessage,
    content: str,
) -> OutboundMessage:
    return OutboundMessage(
        channel=inbound.channel,
        chat_id=inbound.chat_id,
        sender_id=inbound.sender_id,
        session_id=inbound.session_id,
        content=content,
        metadata=inbound.metadata,
    )


def _error_outbound_message(inbound: InboundMessage) -> OutboundMessage:
    return _outbound_message(inbound, _PROCESSING_ERROR_MESSAGE)


def _without_system_messages(messages: tuple[BaseMessage, ...]) -> tuple[BaseMessage, ...]:
    return tuple(message for message in messages if not isinstance(message, SystemMessage))


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
