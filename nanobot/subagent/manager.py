"""Isolated subagent execution and in-memory background task lifecycle."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from ..agent import (
    AgentRunner,
    AgentRunnerError,
    AgentRunResult,
    AgentRunSpec,
    ContextBuilder,
)
from ..bus import InboundMessage, MessageBus
from ..providers import HumanMessage, LLMProvider, SystemMessage
from ..tools import (
    RequestContext,
    ToolContext,
    ToolLoader,
    ToolRegistry,
    bind_request_context,
)

logger = logging.getLogger(__name__)

SubagentTaskStatus = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "cancelled",
    "timeout",
]
_FINAL_TASK_STATUSES = frozenset({"completed", "failed", "cancelled", "timeout"})
_DEFAULT_MAX_BACKGROUND_TASKS = 4
_DEFAULT_BACKGROUND_TIMEOUT_SECONDS = 300.0
_RESULT_SUMMARY_LIMIT = 1_000


@dataclass(frozen=True)
class SubagentRunResult:
    """The isolated result or failure of one subagent task."""

    agent_result: AgentRunResult | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if (self.agent_result is None) == (self.error is None):
            raise ValueError("SubagentRunResult requires exactly one outcome")
        if self.error is not None and (not isinstance(self.error, str) or not self.error):
            raise ValueError("SubagentRunResult error must be a non-empty string")

    @property
    def success(self) -> bool:
        """Return whether the task completed with an AgentRunResult."""

        return self.agent_result is not None


@dataclass(frozen=True)
class BackgroundSubagentTask:
    """In-memory state and route information for one background subagent task."""

    task_id: str
    task: str
    parent_session_key: str
    channel: str
    chat_id: str
    sender_id: str
    metadata: Mapping[str, Any]
    created_at: datetime
    status: SubagentTaskStatus = "pending"
    finished_at: datetime | None = None
    error: str | None = None
    result_summary: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("task_id", self.task_id),
            ("task", self.task),
            ("parent_session_key", self.parent_session_key),
            ("channel", self.channel),
            ("chat_id", self.chat_id),
            ("sender_id", self.sender_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Background subagent task {name} must be non-empty")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("BackgroundSubagentTask metadata must be a mapping")
        if self.status not in {
            "pending",
            "running",
            "completed",
            "failed",
            "cancelled",
            "timeout",
        }:
            raise ValueError(f"Unknown background subagent task status: {self.status}")
        if not isinstance(self.created_at, datetime):
            raise TypeError("Background subagent task created_at must be a datetime")
        if self.finished_at is not None and not isinstance(self.finished_at, datetime):
            raise TypeError("Background subagent task finished_at must be a datetime or None")
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def is_final(self) -> bool:
        """Return whether the task cannot transition again."""

        return self.status in _FINAL_TASK_STATUSES


class SubagentManager:
    """Run isolated subagent work and manage its in-memory background lifecycle."""

    def __init__(
        self,
        runner: AgentRunner,
        provider: LLMProvider,
        context_builder: ContextBuilder,
        tool_context: ToolContext,
        tool_loader: ToolLoader | None = None,
        message_bus: MessageBus | None = None,
        *,
        max_background_tasks: int = _DEFAULT_MAX_BACKGROUND_TASKS,
        background_timeout_seconds: float | None = _DEFAULT_BACKGROUND_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(runner, AgentRunner):
            raise TypeError("SubagentManager requires an AgentRunner")
        if not isinstance(provider, LLMProvider):
            raise TypeError("SubagentManager requires an LLMProvider")
        if not isinstance(context_builder, ContextBuilder):
            raise TypeError("SubagentManager requires a ContextBuilder")
        if not isinstance(tool_context, ToolContext):
            raise TypeError("SubagentManager requires a ToolContext")
        if tool_loader is not None and not isinstance(tool_loader, ToolLoader):
            raise TypeError("SubagentManager tool_loader must be a ToolLoader or None")
        if message_bus is not None and not isinstance(message_bus, MessageBus):
            raise TypeError("SubagentManager message_bus must be a MessageBus or None")
        if not isinstance(max_background_tasks, int) or isinstance(
            max_background_tasks,
            bool,
        ):
            raise TypeError("max_background_tasks must be an integer")
        if max_background_tasks <= 0:
            raise ValueError("max_background_tasks must be positive")
        if background_timeout_seconds is not None:
            if isinstance(background_timeout_seconds, bool) or not isinstance(
                background_timeout_seconds,
                (int, float),
            ):
                raise TypeError("background_timeout_seconds must be a number or None")
            if background_timeout_seconds <= 0:
                raise ValueError("background_timeout_seconds must be positive")

        self._runner = runner
        self._provider = provider
        self._context_builder = context_builder
        self._tool_registry = ToolRegistry()
        (tool_loader or ToolLoader()).load(self._tool_registry, tool_context)
        self._message_bus = message_bus
        self._max_background_tasks = max_background_tasks
        self._background_timeout_seconds = background_timeout_seconds
        self._background_tasks: dict[str, asyncio.Task[None]] = {}
        self._background_task_records: dict[str, BackgroundSubagentTask] = {}
        self._published_task_ids: set[str] = set()
        self._closed = False

    @property
    def running_tasks(self) -> tuple[BackgroundSubagentTask, ...]:
        """Return active tasks in their creation order."""

        return tuple(
            task
            for task in self._background_task_records.values()
            if not task.is_final
        )

    def get_task(self, task_id: str) -> BackgroundSubagentTask | None:
        """Return one task record, including completed records, when known."""

        if not isinstance(task_id, str):
            raise TypeError("task_id must be a string")
        return self._background_task_records.get(task_id)

    def list_tasks(self, session_key: str) -> tuple[BackgroundSubagentTask, ...]:
        """Return all task records owned by one parent session."""

        if not isinstance(session_key, str) or not session_key.strip():
            raise ValueError("session_key must be a non-empty string")
        return tuple(
            task
            for task in self._background_task_records.values()
            if task.parent_session_key == session_key
        )

    def cancel_background(self, task_id: str, *, session_key: str) -> bool:
        """Cancel one active task only when it belongs to the supplied session."""

        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id must be a non-empty string")
        if not isinstance(session_key, str) or not session_key.strip():
            raise ValueError("session_key must be a non-empty string")
        task = self._background_task_records.get(task_id)
        if task is None or task.parent_session_key != session_key or task.is_final:
            return False

        transitioned = self._finish_task(
            task_id,
            "cancelled",
            error="Cancelled by the user.",
        )
        if not transitioned:
            return False
        background_task = self._background_tasks.get(task_id)
        if background_task is not None and not background_task.done():
            background_task.cancel()
        logger.info("Background subagent task cancelled (task_id=%s)", task_id)
        return True

    def start_background(
        self,
        task: str,
        *,
        request_context: RequestContext,
    ) -> str:
        """Start one isolated task and publish its result as an internal message."""

        if not isinstance(task, str) or not task.strip():
            raise ValueError("Subagent task must be a non-empty string")
        if not isinstance(request_context, RequestContext):
            raise TypeError("Subagent request_context must be a RequestContext")
        if self._message_bus is None:
            raise RuntimeError("Background subagent tasks require a MessageBus")
        if self._closed:
            raise RuntimeError("SubagentManager has already been closed")
        if len(self.running_tasks) >= self._max_background_tasks:
            raise RuntimeError("Background subagent task limit has been reached")

        task_id = uuid4().hex
        record = BackgroundSubagentTask(
            task_id=task_id,
            task=task.strip(),
            parent_session_key=request_context.session_key,
            channel=request_context.channel,
            chat_id=request_context.chat_id,
            sender_id=request_context.sender_id,
            metadata=request_context.metadata,
            created_at=_utc_now(),
        )
        self._background_task_records[task_id] = record
        background_task = asyncio.create_task(
            self._run_background(record.task, request_context, task_id)
        )
        self._background_tasks[task_id] = background_task
        background_task.add_done_callback(
            lambda completed_task: self._remove_background_task(task_id, completed_task)
        )
        logger.info("Background subagent task started (task_id=%s)", task_id)
        return task_id

    async def close(self) -> None:
        """Cancel and await every background task still owned by this manager."""

        if self._closed:
            return
        self._closed = True
        for task in tuple(self.running_tasks):
            self.cancel_background(task.task_id, session_key=task.parent_session_key)
        background_tasks = tuple(self._background_tasks.values())
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        self._background_tasks.clear()

    async def run(
        self,
        task: str,
        *,
        request_context: RequestContext | None = None,
    ) -> SubagentRunResult:
        """Run one task without parent history, sessions, or message routing."""

        if not isinstance(task, str) or not task.strip():
            raise ValueError("Subagent task must be a non-empty string")
        if request_context is not None and not isinstance(request_context, RequestContext):
            raise TypeError("Subagent request_context must be a RequestContext or None")

        spec = AgentRunSpec(
            messages=(
                SystemMessage(
                    content=self._context_builder.build_subagent_system_prompt()
                ),
                HumanMessage(content=task),
            ),
            provider=self._provider,
            tool_registry=self._tool_registry,
        )
        try:
            if request_context is None:
                result = await self._runner.run(spec)
            else:
                with bind_request_context(request_context):
                    result = await self._runner.run(spec)
        except asyncio.CancelledError:
            raise
        except AgentRunnerError:
            logger.warning("Subagent run did not complete")
            return SubagentRunResult(error="Subagent could not complete the task.")
        except Exception as exc:
            logger.exception("Subagent execution failed")
            return SubagentRunResult(
                error=f"Subagent execution failed ({type(exc).__name__})."
            )

        if not isinstance(result, AgentRunResult):
            logger.error("Subagent runner returned an invalid result")
            return SubagentRunResult(error="Subagent returned an invalid result.")
        return SubagentRunResult(agent_result=result)

    async def _run_background(
        self,
        task: str,
        request_context: RequestContext,
        task_id: str,
    ) -> None:
        """Run one task and return its terminal outcome through MessageBus once."""

        if not self._mark_running(task_id):
            return
        try:
            result = await self._run_with_timeout(task, request_context)
        except TimeoutError:
            if self._finish_task(
                task_id,
                "timeout",
                error="Subagent task exceeded its time limit.",
            ):
                await self._publish_terminal_result(task_id)
        except asyncio.CancelledError:
            self._finish_task(
                task_id,
                "cancelled",
                error="Subagent task was cancelled.",
            )
            raise
        except Exception as exc:
            logger.exception(
                "Background subagent task failed unexpectedly (task_id=%s)",
                task_id,
            )
            if self._finish_task(
                task_id,
                "failed",
                error=f"Subagent execution failed ({type(exc).__name__}).",
            ):
                await self._publish_terminal_result(task_id)
        else:
            if result.success and result.agent_result is not None:
                completed = self._finish_task(
                    task_id,
                    "completed",
                    result_summary=_result_summary(result.agent_result.content),
                )
            else:
                completed = self._finish_task(
                    task_id,
                    "failed",
                    error=result.error or "Subagent could not complete the task.",
                )
            if completed:
                await self._publish_terminal_result(task_id)

    async def _run_with_timeout(
        self,
        task: str,
        request_context: RequestContext,
    ) -> SubagentRunResult:
        if self._background_timeout_seconds is None:
            return await self.run(task, request_context=request_context)
        return await asyncio.wait_for(
            self.run(task, request_context=request_context),
            timeout=self._background_timeout_seconds,
        )

    async def _publish_terminal_result(self, task_id: str) -> None:
        """Publish one terminal notification; later state changes cannot duplicate it."""

        task = self._background_task_records.get(task_id)
        if task is None or task.status == "cancelled" or task_id in self._published_task_ids:
            return
        if self._message_bus is None:
            raise RuntimeError("Background subagent tasks require a MessageBus")
        self._published_task_ids.add(task_id)
        try:
            await self._message_bus.publish_inbound(_background_result_message(task))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Background subagent result could not be published (task_id=%s)",
                task_id,
            )
            return
        logger.info(
            "Background subagent task result published (task_id=%s, status=%s)",
            task_id,
            task.status,
        )

    def _mark_running(self, task_id: str) -> bool:
        task = self._background_task_records.get(task_id)
        if task is None or task.status != "pending":
            return False
        self._background_task_records[task_id] = replace(task, status="running")
        return True

    def _finish_task(
        self,
        task_id: str,
        status: SubagentTaskStatus,
        *,
        error: str | None = None,
        result_summary: str | None = None,
    ) -> bool:
        """Set exactly one terminal state and retain the resulting task record."""

        if status not in _FINAL_TASK_STATUSES:
            raise ValueError("Background task terminal status is required")
        task = self._background_task_records.get(task_id)
        if task is None or task.is_final:
            return False
        self._background_task_records[task_id] = replace(
            task,
            status=status,
            finished_at=_utc_now(),
            error=error,
            result_summary=result_summary,
        )
        return True

    def _remove_background_task(
        self,
        task_id: str,
        task: asyncio.Task[None],
    ) -> None:
        if self._background_tasks.get(task_id) is task:
            self._background_tasks.pop(task_id, None)


def _background_result_message(task: BackgroundSubagentTask) -> InboundMessage:
    if task.status == "completed":
        content = (
            "A background subagent task has completed. Use its result to respond "
            "to the user:\n\n"
            f"{task.result_summary or 'The subagent completed without a final response.'}"
        )
    elif task.status == "timeout":
        content = (
            "A background subagent task timed out. Explain that the requested "
            "task could not be completed in time."
        )
    else:
        content = (
            "A background subagent task could not complete. Explain the failure "
            "to the user if needed:\n\n"
            f"{task.error or 'Subagent could not complete the task.'}"
        )
    return InboundMessage(
        channel=task.channel,
        chat_id=task.chat_id,
        sender_id=task.sender_id,
        session_id=task.parent_session_key,
        content=content,
        metadata={
            **task.metadata,
            "source": "subagent",
            "task_id": task.task_id,
            "parent_session_key": task.parent_session_key,
            "subagent_status": task.status,
        },
    )


def _result_summary(content: str | None) -> str | None:
    if content is None:
        return None
    summary = content.strip()
    if len(summary) <= _RESULT_SUMMARY_LIMIT:
        return summary or None
    return f"{summary[:_RESULT_SUMMARY_LIMIT - 3]}..."


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
