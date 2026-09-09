"""Command parsing and session-control handlers for the Agent loop."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..bus import InboundMessage, MessageBus, OutboundMessage
from ..memory import MemoryStore
from ..session import Session, SessionCompactor, SessionManager
from ..session.goals import build_goal_start_content

if TYPE_CHECKING:
    from ..subagent import BackgroundSubagentTask, SubagentManager

logger = logging.getLogger(__name__)

DEFAULT_MEMORY_OUTPUT_LIMIT = 4_000


@dataclass(frozen=True)
class CommandInvocation:
    """A parsed slash command name and its whitespace-separated arguments."""

    name: str
    arguments: tuple[str, ...]


@dataclass(frozen=True)
class CommandContext:
    """The current routed message and explicit capabilities available to a handler."""

    message: InboundMessage
    session_key: str
    session: Session | None
    command: CommandInvocation
    session_manager: SessionManager
    session_compactor: SessionCompactor | None
    memory_store: MemoryStore | None
    subagent_manager: SubagentManager | None
    cancel_active_turn: Callable[[], bool | Awaitable[bool]]
    cancel_goal_turn: Callable[[], bool]


CommandHandler = Callable[[CommandContext], Awaitable[str]]


@dataclass(frozen=True)
class _RegisteredCommand:
    description: str
    handler: CommandHandler
    accepts_arguments: bool = False


class CommandRouter:
    """Parse, dispatch, and describe the small built-in Agent command set."""

    def __init__(
        self,
        session_manager: SessionManager,
        *,
        session_compactor: SessionCompactor | None = None,
        memory_store: MemoryStore | None = None,
        subagent_manager: SubagentManager | None = None,
        message_bus: MessageBus | None = None,
        memory_output_limit: int = DEFAULT_MEMORY_OUTPUT_LIMIT,
        cancel_active_turn: Callable[[str], bool | Awaitable[bool]] | None = None,
        cancel_goal_turn: Callable[[str], bool] | None = None,
    ) -> None:
        if not isinstance(session_manager, SessionManager):
            raise TypeError("CommandRouter requires a SessionManager")
        if session_compactor is not None and not isinstance(
            session_compactor,
            SessionCompactor,
        ):
            raise TypeError("session_compactor must be a SessionCompactor or None")
        if memory_store is not None and not isinstance(memory_store, MemoryStore):
            raise TypeError("memory_store must be a MemoryStore or None")
        if message_bus is not None and not isinstance(message_bus, MessageBus):
            raise TypeError("message_bus must be a MessageBus or None")
        if not isinstance(memory_output_limit, int) or isinstance(
            memory_output_limit,
            bool,
        ):
            raise TypeError("memory_output_limit must be an integer")
        if memory_output_limit <= 0:
            raise ValueError("memory_output_limit must be positive")

        self._session_manager = session_manager
        self._session_compactor = session_compactor
        self._memory_store = memory_store
        self._subagent_manager = subagent_manager
        self._message_bus = message_bus
        self._memory_output_limit = memory_output_limit
        self._cancel_active_turn = cancel_active_turn or (lambda _session_key: False)
        self._cancel_goal_turn = cancel_goal_turn or (lambda _session_key: False)
        self._commands: dict[str, _RegisteredCommand] = {}
        self.register("new", "清空当前会话的短期历史。", self._handle_new)
        self.register("stop", "停止当前会话正在执行的请求。", self._handle_stop)
        self.register("help", "显示可用命令。", self._handle_help)
        self.register("goal", "创建当前会话的持续目标。", self._handle_goal, accepts_arguments=True)
        self.register("compact", "整理当前会话的较早历史摘要。", self._handle_compact)
        self.register("memory", "查看当前 workspace 的长期记忆。", self._handle_memory)
        if subagent_manager is not None:
            self.register(
                "subagents",
                "查看、查询或取消当前会话的后台子 Agent 任务。",
                self._handle_subagents,
                accepts_arguments=True,
            )

    @property
    def command_names(self) -> tuple[str, ...]:
        """Return registered command names in stable registration order."""

        return tuple(self._commands)

    def register(
        self,
        name: str,
        description: str,
        handler: CommandHandler,
        *,
        accepts_arguments: bool = False,
    ) -> None:
        """Register one command without silently replacing an existing name."""

        normalized_name = _normalize_command_name(name)
        if normalized_name in self._commands:
            raise ValueError(f"Command is already registered: {normalized_name}")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("Command description must be a non-empty string")
        if not callable(handler):
            raise TypeError("Command handler must be callable")
        if not isinstance(accepts_arguments, bool):
            raise TypeError("accepts_arguments must be a bool")
        self._commands[normalized_name] = _RegisteredCommand(
            description=description.strip(),
            handler=handler,
            accepts_arguments=accepts_arguments,
        )

    def parse(self, content: str) -> CommandInvocation | None:
        """Parse a slash command, or return ``None`` for ordinary text."""

        if not isinstance(content, str):
            raise TypeError("Command content must be a string")
        stripped = content.strip()
        if not stripped.startswith("/"):
            return None
        parts = stripped[1:].split()
        if not parts:
            return CommandInvocation(name="", arguments=())
        return CommandInvocation(
            name=parts[0].lower(),
            arguments=tuple(parts[1:]),
        )

    def is_stop_command(self, invocation: CommandInvocation | None) -> bool:
        """Return whether an invocation should bypass the session lock."""

        return invocation is not None and invocation.name == "stop"

    def is_goal_command(self, invocation: CommandInvocation | None) -> bool:
        """Return whether an invocation controls the current session goal."""

        return invocation is not None and invocation.name == "goal"

    async def route(
        self,
        message: InboundMessage,
        session_key: str,
        session: Session | None,
        invocation: CommandInvocation | None = None,
    ) -> OutboundMessage | None:
        """Dispatch a command and return its routed reply, if the message is a command."""

        if not isinstance(message, InboundMessage):
            raise TypeError("CommandRouter requires an InboundMessage")
        if not isinstance(session_key, str) or not session_key.strip():
            raise ValueError("session_key must be a non-empty string")
        if session is not None and not isinstance(session, Session):
            raise TypeError("session must be a Session or None")

        command = invocation if invocation is not None else self.parse(message.content)
        if command is None:
            return None
        if not command.name:
            return self._reply(message, "命令格式错误，请使用 /help 查看可用命令。")

        registered = self._commands.get(command.name)
        if registered is None:
            return self._reply(
                message,
                f"未知命令：/{command.name}。\n\n{self._help_text()}",
            )
        if command.arguments and not registered.accepts_arguments:
            return self._reply(
                message,
                f"/{command.name} 不接受参数。请使用 /help 查看用法。",
            )

        context = CommandContext(
            message=message,
            session_key=session_key,
            session=session,
            command=command,
            session_manager=self._session_manager,
            session_compactor=self._session_compactor,
            memory_store=self._memory_store,
            subagent_manager=self._subagent_manager,
            cancel_active_turn=lambda: self._cancel_active_turn(session_key),
            cancel_goal_turn=lambda: self._cancel_goal_turn(session_key),
        )
        try:
            content = await registered.handler(context)
            if not isinstance(content, str):
                raise TypeError("Command handler must return a string")
            return self._reply(message, content)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Command handler failed (name=%s)", command.name)
            return self._reply(message, "命令执行失败，请稍后重试。")

    async def _handle_new(self, context: CommandContext) -> str:
        session = _require_session(context)
        if session.goal_state is not None and session.goal_state.status == "active":
            return (
                f"当前会话存在进行中的目标：{session.goal_state.objective}。"
                "请等待目标完成，或先使用 /goal stop 取消目标后再开始新会话。"
            )
        context.session_manager.save(session.reset())
        return "已开始新的会话，当前会话的短期历史已清空。"

    async def _handle_stop(self, context: CommandContext) -> str:
        cancelled = context.cancel_active_turn()
        if inspect.isawaitable(cancelled):
            cancelled = await cancelled
        if cancelled:
            return "已请求停止当前会话的执行。"
        return "当前会话没有正在执行的请求。"

    async def _handle_help(self, context: CommandContext) -> str:
        del context
        return self._help_text()

    async def _handle_goal(self, context: CommandContext) -> str:
        """Create one active goal without entering the normal Agent turn flow."""

        arguments = context.command.arguments
        action = arguments[0].lower() if arguments else None
        session = _require_session(context)
        if action in {"status", "stop"}:
            if len(arguments) != 1:
                return "用法：/goal status 或 /goal stop"
            if action == "status":
                current = session.goal_state
                if current is None:
                    return "当前会话没有目标。"
                return f"当前目标状态：{current.status}\n目标：{current.objective}"
            try:
                updated_session = context.session_manager.update_goal(
                    session,
                    cancel=True,
                )
            except ValueError as error:
                return str(error)
            context.cancel_goal_turn()
            goal = updated_session.goal_state
            if goal is None:
                raise RuntimeError("Cancelling a goal must retain terminal goal state")
            return f"已停止目标：{goal.objective}"

        objective = " ".join(arguments).strip()
        if not objective:
            return "用法：/goal <目标描述>、/goal status 或 /goal stop"

        try:
            updated_session = context.session_manager.create_goal(session, objective)
        except ValueError as error:
            return str(error)
        goal = updated_session.goal_state
        if goal is None:
            raise RuntimeError("Creating a goal must persist active goal state")
        if self._message_bus is not None:
            await self._message_bus.publish_inbound(
                _goal_start_message(context.message, context.session_key, goal.objective)
            )
        return f"已创建目标：{goal.objective}\n\n已开始执行目标。"

    async def _handle_compact(self, context: CommandContext) -> str:
        session = _require_session(context)
        compactor = context.session_compactor
        if compactor is None:
            return "当前未启用会话摘要压缩。"
        compacted = await compactor.compact_manually(session)
        if compacted is session:
            return "当前会话没有可压缩的完整历史。"
        context.session_manager.save(compacted)
        return "当前会话历史已整理为摘要。"

    async def _handle_memory(self, context: CommandContext) -> str:
        store = context.memory_store
        if store is None:
            return "当前未配置长期记忆。"
        content = store.read()
        if not content:
            return "当前 workspace 还没有长期记忆。"
        if len(content) > self._memory_output_limit:
            content = f"{content[:self._memory_output_limit]}\n\n（内容已截断）"
        return content

    async def _handle_subagents(self, context: CommandContext) -> str:
        """List, inspect, or cancel background tasks owned by this session."""

        manager = context.subagent_manager
        if manager is None:
            return "当前未启用后台子 Agent。"
        arguments = context.command.arguments
        if not arguments:
            tasks = manager.list_tasks(context.session_key)
            if not tasks:
                return "当前会话没有后台子 Agent 任务。"
            return "\n".join(_format_subagent_task(task) for task in tasks)

        action = arguments[0].lower()
        if action not in {"status", "cancel"} or len(arguments) != 2:
            return "用法：/subagents、/subagents status <task_id> 或 /subagents cancel <task_id>。"

        task = manager.get_task(arguments[1])
        if task is None or task.parent_session_key != context.session_key:
            return "未找到当前会话的子 Agent 任务。"
        if action == "status":
            return _format_subagent_task(task, include_result=True)
        if task.is_final:
            return f"任务 {task.task_id} 已处于 {task.status} 状态，不能再次取消。"
        if not manager.cancel_background(task.task_id, session_key=context.session_key):
            return "子 Agent 任务无法取消，请稍后重试。"
        return f"已请求取消子 Agent 任务 {task.task_id}。"

    def _help_text(self) -> str:
        lines = ["可用命令："]
        lines.extend(
            f"- /{name}：{registered.description}"
            for name, registered in self._commands.items()
        )
        return "\n".join(lines)

    @staticmethod
    def _reply(message: InboundMessage, content: str) -> OutboundMessage:
        return OutboundMessage(
            channel=message.channel,
            chat_id=message.chat_id,
            sender_id=message.sender_id,
            session_id=message.session_id,
            content=content,
            metadata=message.metadata,
        )


def _normalize_command_name(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError("Command name must be a string")
    normalized = name.strip().lower().removeprefix("/")
    if not normalized or any(character.isspace() for character in normalized):
        raise ValueError("Command name must be a non-empty single word")
    return normalized


def _require_session(context: CommandContext) -> Session:
    if context.session is None:
        raise RuntimeError("Command requires a session")
    return context.session


def _goal_start_message(
    message: InboundMessage,
    session_key: str,
    objective: str,
) -> InboundMessage:
    """Build the internal turn that begins work on a newly saved goal."""

    return InboundMessage(
        channel=message.channel,
        chat_id=message.chat_id,
        sender_id=message.sender_id,
        session_id=session_key,
        content=build_goal_start_content(objective),
        metadata={
            **message.metadata,
            "source": "goal",
        },
    )


def _format_subagent_task(
    task: BackgroundSubagentTask,
    *,
    include_result: bool = False,
) -> str:
    """Format the small task record shown by the command router."""

    detail = task.task
    if include_result and task.result_summary:
        detail = task.result_summary
    elif include_result and task.error:
        detail = task.error
    return f"- {task.task_id} · {task.status} · {detail}"
