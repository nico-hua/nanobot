"""Single-turn orchestration around the minimal agent runner."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Literal

from ..bus import InboundMessage, MessageBus, OutboundMessage
from ..memory import MemoryConsolidator, MemoryEventConsumer, MemoryStore
from ..providers import BaseMessage, HumanMessage, LLMProvider, SystemMessage
from ..session import Session, SessionCompactor, SessionManager
from ..session.goals import build_goal_continuation_content
from ..tools import RequestContext, ToolRegistry, bind_request_context
from .commands import CommandInvocation, CommandRouter
from .context import ContextBuilder, ContextWindowExceededError
from .runner import AgentRunner, AgentRunResult, AgentRunSpec

if TYPE_CHECKING:
    from ..subagent import SubagentManager

logger = logging.getLogger(__name__)

_CONTEXT_WINDOW_EXCEEDED_MESSAGE = (
    "当前会话的系统提示词、摘要或当前消息已超过模型上下文窗口，"
    "请新开会话或缩短消息后重试。"
)
_PROCESSING_ERROR_MESSAGE = "处理消息时发生错误，请稍后重试。"
_MAX_ITERATIONS_MESSAGE = "该智能体在完成此请求之前达到了迭代次数上限。"
_GOAL_CONTINUATION_LIMIT_MESSAGE = (
    "该目标达到了自动延续限制，并被标记为失败。"
)
_GOAL_CONTINUATION_UNAVAILABLE_MESSAGE = (
    "该目标无法继续，并被标记为失败。"
)
DEFAULT_MAX_GOAL_CONTINUATIONS = 10
_GOAL_INJECTION_TEMPLATE = """[New user input during goal execution]

The user sent the following new input while the active goal is running:

{messages}

Continue the active goal while incorporating this input. If changing or stopping the
goal is necessary, use the update_goal tool with action update or stop.
"""


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
        subagent_manager: SubagentManager | None = None,
        max_iterations: int = 30,
        max_goal_continuations: int = DEFAULT_MAX_GOAL_CONTINUATIONS,
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
        if not isinstance(max_iterations, int) or isinstance(max_iterations, bool):
            raise TypeError("AgentLoop max_iterations must be an integer")
        if max_iterations <= 0:
            raise ValueError("AgentLoop max_iterations must be positive")
        if not isinstance(max_goal_continuations, int) or isinstance(
            max_goal_continuations,
            bool,
        ):
            raise TypeError("AgentLoop max_goal_continuations must be an integer")
        if max_goal_continuations < 0:
            raise ValueError("AgentLoop max_goal_continuations must not be negative")

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
        self._subagent_manager = subagent_manager
        self._max_iterations = max_iterations
        self._max_goal_continuations = max_goal_continuations
        self._command_router = command_router or CommandRouter(
            session_manager,
            session_compactor=session_compactor,
            memory_store=memory_store,
            subagent_manager=subagent_manager,
            message_bus=message_bus,
            cancel_active_turn=self._cancel_active_turn,
            cancel_goal_turn=self._cancel_goal_turn,
        )
        # 同一 session 的写入、压缩和命令操作必须按顺序执行。
        self._session_locks: dict[str, asyncio.Lock] = {}
        # 普通 turn 在真正开始前就登记，保证紧随其后的 /stop 也能取消它。
        self._active_turn_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        # source=goal 的内部 turn 在执行期间独占目标模式；/goal 控制命令可据此绕过 session lock。
        self._goal_turn_tasks: dict[str, asyncio.Task[Any]] = {}
        # 目标运行期间的普通用户输入只由当前 Runner 在安全检查点注入。
        self._pending_user_messages: dict[str, asyncio.Queue[InboundMessage]] = {}
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
                session_key = _session_key(
                    inbound.channel,
                    inbound.chat_id,
                    inbound.session_id,
                )
                if self._command_router.is_stop_command(invocation):
                    # /stop 不能等待当前 session 的锁，否则无法停止正在运行的 turn。
                    await self._publish_immediate_command(
                        inbound,
                        invocation,
                        session=None,
                        operation="/stop",
                    )
                    continue
                if self._is_goal_mode_message(
                    inbound,
                    invocation,
                    session_key,
                ):
                    await self._handle_goal_mode_message(
                        inbound,
                        invocation,
                        session_key,
                    )
                    continue

                # MessageBus is the inbound queue; remaining messages are
                # handled asynchronously and remain cancellable by /stop.
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
        if self._subagent_manager is not None:
            await self._subagent_manager.close()
        # Application 关闭时统一取消请求、压缩和记忆消费者，避免遗留后台任务。
        tasks = set(self._compaction_tasks)
        tasks.update(self._inbound_tasks)
        for turn_tasks in self._active_turn_tasks.values():
            tasks.update(turn_tasks)
        tasks.update(self._goal_turn_tasks.values())
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
        self._goal_turn_tasks.clear()
        self._pending_user_messages.clear()

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

    async def _publish_immediate_command(
        self,
        inbound: InboundMessage,
        invocation: CommandInvocation,
        *,
        session: Session | None,
        operation: str,
    ) -> None:
        """Route and publish a command that must bypass the session lock."""

        try:
            response = await self._route_command(inbound, invocation, session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Agent loop failed while handling %s", operation)
            response = _outbound_message(inbound, _PROCESSING_ERROR_MESSAGE)

        # Only ``run`` calls this method, after it has established the bus.
        if self._message_bus is None:
            raise RuntimeError("AgentLoop requires a MessageBus to publish a command")
        await self._message_bus.publish_outbound(response)

    def _enqueue_message(
        self,
        inbound: InboundMessage,
        invocation: CommandInvocation | None,
    ) -> None:
        """Schedule one non-stop queue item while making normal turns stoppable."""

        session_key = _session_key(inbound.channel, inbound.chat_id, inbound.session_id)
        task = asyncio.create_task(self._process_queued_message(inbound, invocation))
        self._inbound_tasks.add(task)
        task.add_done_callback(self._inbound_tasks.discard)
        if invocation is None:
            # 在 task 获得运行机会前先登记，处理“普通消息后立刻收到 /stop”的竞态。
            self._track_turn_task(
                session_key,
                task,
            )
            if _is_goal_message(inbound):
                self._track_goal_turn_task(
                    session_key,
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
            response = _outbound_message(inbound, _PROCESSING_ERROR_MESSAGE)
        if response is not None:
            await self._message_bus.publish_outbound(response)

    # -- Message dispatch -------------------------------------------------

    async def _run_stop_command(
        self,
        inbound: InboundMessage,
        invocation: CommandInvocation,
    ) -> OutboundMessage:
        """Run the one command that must not wait for the session lock."""

        return await self._route_command(inbound, invocation, session=None)

    async def _route_command(
        self,
        inbound: InboundMessage,
        invocation: CommandInvocation,
        session: Session | None,
    ) -> OutboundMessage:
        """Route a parsed command with the caller-selected session locking policy."""

        session_key = _session_key(inbound.channel, inbound.chat_id, inbound.session_id)
        return _require_command_reply(
            await self._command_router.route(
                inbound,
                session_key,
                session,
                invocation,
            )
        )

    async def _dispatch_non_stop_message(
        self,
        inbound: InboundMessage,
        invocation: CommandInvocation | None,
    ) -> OutboundMessage | None:
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
            return await self._route_command(inbound, invocation, session)

    # -- Normal Agent turn ------------------------------------------------

    async def _run_turn(
        self,
        inbound: InboundMessage,
        session_key: str,
    ) -> OutboundMessage | None:
        """Run one turn and persist its completed message sequence."""

        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("AgentLoop requires a running asyncio task")
        is_goal_turn = _is_goal_message(inbound)
        if is_goal_turn and not self._has_active_goal(session_key):
            # A new /goal persists its active GoalState before publishing this
            # message. Therefore this only rejects stale source=goal messages
            # after a completed, cancelled, or failed goal.
            logger.info("Ignoring goal turn because its goal is no longer active")
            return None
        self._track_turn_task(session_key, task)
        if is_goal_turn:
            self._track_goal_turn_task(session_key, task)
        try:
            # 锁覆盖“读取历史 -> 请求模型 -> 保存结果”，防止同一会话交错写入。
            async with self._lock_for(session_key):
                session = self._session_manager.get_or_create(session_key)
                history = _without_system_messages(session.messages)
                current_message = HumanMessage(content=inbound.content)
                blocked_tool_names = _blocked_tool_names(is_goal_turn)
                available_tools = tuple(
                    tool
                    for tool in self._tool_registry.tools
                    if tool.name not in blocked_tool_names
                )
                try:
                    request_messages = self._context_builder.build_request_messages(
                        history,
                        current_message,
                        summary=session.summary,
                        summary_until=session.summary_until,
                        tools=available_tools,
                    )
                except ContextWindowExceededError:
                    logger.warning("Agent request exceeds the configured context window")
                    if is_goal_turn:
                        self._finish_active_goal(session_key, "failed")
                    return _outbound_message(
                        inbound,
                        _CONTEXT_WINDOW_EXCEEDED_MESSAGE,
                    )

                spec = AgentRunSpec(
                    messages=request_messages,
                    provider=self._provider,
                    tool_registry=self._tool_registry,
                    max_iterations=self._max_iterations,
                    blocked_tool_names=blocked_tool_names,
                    is_goal_mode=is_goal_turn,
                    injection_callback=(
                        lambda: self._take_goal_user_messages(session_key)
                    )
                    if is_goal_turn
                    else None,
                )
                request_context = RequestContext(
                    session_key=session_key,
                    channel=inbound.channel,
                    chat_id=inbound.chat_id,
                    sender_id=inbound.sender_id,
                    metadata=inbound.metadata,
                    is_goal_mode=is_goal_turn,
                )
                with bind_request_context(request_context):
                    result = await self._runner.run(spec)
                completed_messages = _without_system_messages(
                    result.messages[len(spec.messages) :]
                )
                saved_session = self._persist_completed_turn(
                    session_key,
                    history,
                    current_message,
                    completed_messages,
                    is_goal_turn=is_goal_turn,
                    result=result,
                )
                if is_goal_turn and result.stop_reason == "max_iterations":
                    return await self._schedule_goal_continuation(
                        inbound,
                        session_key,
                        saved_session,
                    )
                if result.stop_reason == "max_iterations":
                    return _outbound_message(inbound, _MAX_ITERATIONS_MESSAGE)
                return _outbound_message(inbound, result.content or "")
        except asyncio.CancelledError:
            if is_goal_turn:
                self._finish_active_goal(session_key, "failed")
            raise
        except Exception:
            if is_goal_turn:
                self._finish_active_goal(session_key, "failed")
            raise
        finally:
            self._untrack_turn_task(session_key, task)
            if is_goal_turn:
                self._untrack_goal_turn_task(session_key, task)

    def _persist_completed_turn(
        self,
        session_key: str,
        history: tuple[BaseMessage, ...],
        current_message: HumanMessage,
        completed_messages: tuple[BaseMessage, ...],
        *,
        is_goal_turn: bool,
        result: AgentRunResult,
    ) -> Session:
        """Save one complete runner result, then trigger post-save work."""

        # A session is only changed after AgentRunner has returned a complete
        # assistant/tool sequence.  This keeps cancellation from persisting a
        # partial tool-call batch.
        session = self._session_manager.get_or_create(session_key).with_messages(
            (*history, current_message, *completed_messages)
        )
        if is_goal_turn and result.stop_reason != "max_iterations":
            status: Literal["completed", "failed"] = (
                "completed"
                if result.content is not None and result.content.strip()
                else "failed"
            )
            session = self._finish_active_goal_state(session, status)

        saved_session = self._session_manager.save(session)
        if self._memory_events is not None:
            self._memory_events.append(
                session_key,
                (current_message, *completed_messages),
            )
        # Compaction is deliberately asynchronous so it cannot delay a reply.
        self._schedule_compaction(session_key)
        return saved_session

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

    def _track_goal_turn_task(self, session_key: str, task: asyncio.Task[Any]) -> None:
        if self._goal_turn_tasks.get(session_key) is task:
            return
        self._goal_turn_tasks[session_key] = task
        task.add_done_callback(
            lambda completed_task: self._untrack_goal_turn_task(
                session_key,
                completed_task,
            )
        )

    def _untrack_turn_task(self, session_key: str, task: asyncio.Task[Any]) -> None:
        tasks = self._active_turn_tasks.get(session_key)
        if tasks is None:
            return
        tasks.discard(task)
        if not tasks:
            self._active_turn_tasks.pop(session_key, None)

    def _untrack_goal_turn_task(
        self,
        session_key: str,
        task: asyncio.Task[Any],
    ) -> None:
        if self._goal_turn_tasks.get(session_key) is task:
            self._goal_turn_tasks.pop(session_key, None)
            if not self._has_active_goal(session_key):
                self._pending_user_messages.pop(session_key, None)

    def _start_goal_continuation(
        self,
        inbound: InboundMessage,
        session_key: str,
    ) -> None:
        """Schedule a continuation without treating it as user input."""

        if not self._has_active_goal(session_key):
            logger.info("Ignoring stale internal goal message")
            return
        self._enqueue_message(inbound, None)

    def _is_goal_mode_message(
        self,
        inbound: InboundMessage,
        invocation: CommandInvocation | None,
        session_key: str,
    ) -> bool:
        """Return whether a message has special handling during goal execution."""

        if _is_goal_continuation_message(inbound):
            return True
        if invocation is None:
            return (
                not _is_goal_message(inbound)
                and inbound.metadata.get("source") is None
                and self._is_goal_mode(session_key)
            )
        return (
            self._command_router.is_goal_command(invocation)
            and self._is_goal_mode(session_key)
        )

    async def _handle_goal_mode_message(
        self,
        inbound: InboundMessage,
        invocation: CommandInvocation | None,
        session_key: str,
    ) -> None:
        """Execute the goal-specific path selected by ``_is_goal_mode_message``."""

        # A continuation is consumed even when stale, so it is never treated
        # as ordinary user input.
        if _is_goal_continuation_message(inbound):
            self._start_goal_continuation(inbound, session_key)
            return
        if invocation is None:
            self._queue_goal_user_message(session_key, inbound)
            return

        # Goal commands bypass the session lock held by the active goal turn.
        await self._publish_immediate_command(
            inbound,
            invocation,
            session=self._session_manager.get_or_create(session_key),
            operation="/goal during goal mode",
        )

    def _queue_goal_user_message(
        self,
        session_key: str,
        inbound: InboundMessage,
    ) -> None:
        queue = self._pending_user_messages.setdefault(session_key, asyncio.Queue())
        queue.put_nowait(inbound)

    async def _take_goal_user_messages(
        self,
        session_key: str,
    ) -> tuple[HumanMessage, ...]:
        """Merge queued external input into one user message for the active goal."""

        queue = self._pending_user_messages.get(session_key)
        if queue is None:
            return ()
        messages: list[InboundMessage] = []
        while True:
            try:
                messages.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if queue.empty() and self._pending_user_messages.get(session_key) is queue:
            self._pending_user_messages.pop(session_key, None)
        if not messages:
            return ()
        content = "\n".join(
            f"{index}. {message.content}"
            for index, message in enumerate(messages, start=1)
        )
        return (HumanMessage(content=_GOAL_INJECTION_TEMPLATE.format(messages=content)),)

    def _cancel_active_turn(self, session_key: str) -> bool:
        # Do not let input collected for a cancelled goal leak into a later turn.
        self._pending_user_messages.pop(session_key, None)
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

    def _cancel_goal_turn(self, session_key: str) -> bool:
        self._pending_user_messages.pop(session_key, None)
        task = self._goal_turn_tasks.get(session_key)
        if task is None or task.done():
            return False
        task.cancel()
        logger.info("Cancelling active goal turn")
        return True

    def _is_goal_mode(self, session_key: str) -> bool:
        task = self._goal_turn_tasks.get(session_key)
        return (task is not None and not task.done()) or self._has_active_goal(
            session_key
        )

    def _has_active_goal(self, session_key: str) -> bool:
        goal = self._session_manager.get_or_create(session_key).goal_state
        return goal is not None and goal.status == "active"

    async def _schedule_goal_continuation(
        self,
        inbound: InboundMessage,
        session_key: str,
        session: Session,
    ) -> OutboundMessage | None:
        """Persist and queue one later goal turn after a completed tool boundary."""

        goal = session.goal_state
        if goal is None or goal.status != "active":
            return None
        if goal.continuation_count >= self._max_goal_continuations:
            self._finish_active_goal(session_key, "failed")
            return _outbound_message(inbound, _GOAL_CONTINUATION_LIMIT_MESSAGE)
        if self._message_bus is None:
            self._finish_active_goal(session_key, "failed")
            return _outbound_message(inbound, _GOAL_CONTINUATION_UNAVAILABLE_MESSAGE)

        continued_session = self._session_manager.save(
            session.with_goal_state(goal.record_continuation())
        )
        continued_goal = continued_session.goal_state
        if continued_goal is None:
            raise RuntimeError("Continuing a goal must retain its goal state")
        continuation = InboundMessage(
            channel=inbound.channel,
            chat_id=inbound.chat_id,
            sender_id=inbound.sender_id,
            session_id=session_key,
            content=build_goal_continuation_content(continued_goal.objective),
            metadata={
                **inbound.metadata,
                "source": "goal",
                "goal_continuation": True,
            },
        )
        try:
            await self._message_bus.publish_inbound(continuation)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to schedule goal continuation")
            self._finish_active_goal(session_key, "failed")
            return _outbound_message(inbound, _GOAL_CONTINUATION_UNAVAILABLE_MESSAGE)
        return None

    def _finish_active_goal(
        self,
        session_key: str,
        status: Literal["completed", "failed"],
    ) -> None:
        session = self._session_manager.get_or_create(session_key)
        updated_session = self._finish_active_goal_state(session, status)
        if updated_session is not session:
            self._session_manager.save(updated_session)

    @staticmethod
    def _finish_active_goal_state(
        session: Session,
        status: Literal["completed", "failed"],
    ) -> Session:
        goal = session.goal_state
        if goal is None or goal.status != "active":
            return session
        return session.with_goal_state(goal.finish(status))


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

def _without_system_messages(messages: tuple[BaseMessage, ...]) -> tuple[BaseMessage, ...]:
    return tuple(message for message in messages if not isinstance(message, SystemMessage))


def _is_goal_message(inbound: InboundMessage) -> bool:
    return inbound.metadata.get("source") == "goal"


def _is_goal_continuation_message(inbound: InboundMessage) -> bool:
    return (
        _is_goal_message(inbound)
        and inbound.metadata.get("goal_continuation") is True
    )


def _blocked_tool_names(is_goal_mode: bool) -> tuple[str, ...]:
    """Return the goal tool excluded from the current execution mode."""

    return ("create_goal",) if is_goal_mode else ("update_goal",)
