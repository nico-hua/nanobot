"""One-shot subagent execution without parent history or session state."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
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
    """Route information retained for one background subagent task."""

    task_id: str
    parent_session_key: str
    channel: str
    chat_id: str
    sender_id: str
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, Mapping):
            raise TypeError("BackgroundSubagentTask metadata must be a mapping")
        object.__setattr__(self, "metadata", dict(self.metadata))


class SubagentManager:
    """Run an isolated task context with separately loaded built-in tools."""

    def __init__(
        self,
        runner: AgentRunner,
        provider: LLMProvider,
        context_builder: ContextBuilder,
        tool_context: ToolContext,
        tool_loader: ToolLoader | None = None,
        message_bus: MessageBus | None = None,
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

        self._runner = runner
        self._provider = provider
        self._context_builder = context_builder
        self._tool_registry = ToolRegistry()
        (tool_loader or ToolLoader()).load(self._tool_registry, tool_context)
        self._message_bus = message_bus
        self._background_tasks: dict[str, asyncio.Task[None]] = {}
        self._background_task_sources: dict[str, BackgroundSubagentTask] = {}
        self._closed = False

    @property
    def running_tasks(self) -> tuple[BackgroundSubagentTask, ...]:
        """Return route information for background tasks that have not finished."""

        return tuple(self._background_task_sources.values())

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

        task_id = uuid4().hex
        source = BackgroundSubagentTask(
            task_id=task_id,
            parent_session_key=request_context.session_key,
            channel=request_context.channel,
            chat_id=request_context.chat_id,
            sender_id=request_context.sender_id,
            metadata=request_context.metadata,
        )
        background_task = asyncio.create_task(
            self._run_background(task.strip(), request_context, source)
        )
        self._background_tasks[task_id] = background_task
        self._background_task_sources[task_id] = source
        background_task.add_done_callback(
            lambda completed_task: self._remove_background_task(
                task_id,
                completed_task,
            )
        )
        logger.info("Background subagent task started (task_id=%s)", task_id)
        return task_id

    async def close(self) -> None:
        """Cancel and await every background task still owned by this manager."""

        if self._closed:
            return
        self._closed = True
        tasks = tuple(self._background_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()
        self._background_task_sources.clear()

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
        source: BackgroundSubagentTask,
    ) -> None:
        """Run one task and return its outcome through the shared MessageBus."""

        try:
            result = await self.run(task, request_context=request_context)
            inbound = _background_result_message(source, result)
            await self._publish_background_result(inbound, source.task_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Background subagent task failed unexpectedly (task_id=%s)",
                source.task_id,
            )
            inbound = _background_failure_message(source)
            try:
                await self._publish_background_result(inbound, source.task_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Background subagent failure notification could not be published "
                    "(task_id=%s)",
                    source.task_id,
                )

    async def _publish_background_result(
        self,
        message: InboundMessage,
        task_id: str,
    ) -> None:
        if self._message_bus is None:
            raise RuntimeError("Background subagent tasks require a MessageBus")
        await self._message_bus.publish_inbound(message)
        logger.info("Background subagent task completed (task_id=%s)", task_id)

    def _remove_background_task(
        self,
        task_id: str,
        task: asyncio.Task[None],
    ) -> None:
        if self._background_tasks.get(task_id) is task:
            self._background_tasks.pop(task_id, None)
            self._background_task_sources.pop(task_id, None)


def _background_result_message(
    source: BackgroundSubagentTask,
    result: SubagentRunResult,
) -> InboundMessage:
    if result.success and result.agent_result is not None:
        content = (
            "A background subagent task has completed. Use its result to respond "
            "to the user:\n\n"
            f"{result.agent_result.content or 'The subagent completed without a final response.'}"
        )
        status = "completed"
    else:
        content = (
            "A background subagent task could not complete. Explain the failure "
            "to the user if needed:\n\n"
            f"{result.error or 'Subagent could not complete the task.'}"
        )
        status = "failed"
    return InboundMessage(
        channel=source.channel,
        chat_id=source.chat_id,
        sender_id=source.sender_id,
        session_id=source.parent_session_key,
        content=content,
        metadata={
            **source.metadata,
            "source": "subagent",
            "task_id": source.task_id,
            "parent_session_key": source.parent_session_key,
            "subagent_status": status,
        },
    )


def _background_failure_message(source: BackgroundSubagentTask) -> InboundMessage:
    return InboundMessage(
        channel=source.channel,
        chat_id=source.chat_id,
        sender_id=source.sender_id,
        session_id=source.parent_session_key,
        content=(
            "A background subagent task failed unexpectedly. Explain that the "
            "requested task could not be completed."
        ),
        metadata={
            **source.metadata,
            "source": "subagent",
            "task_id": source.task_id,
            "parent_session_key": source.parent_session_key,
            "subagent_status": "failed",
        },
    )
