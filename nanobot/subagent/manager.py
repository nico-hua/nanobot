"""One-shot subagent execution without parent history or session state."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from ..agent import (
    AgentRunner,
    AgentRunnerError,
    AgentRunResult,
    AgentRunSpec,
    ContextBuilder,
)
from ..providers import HumanMessage, LLMProvider, SystemMessage
from ..tools import ToolContext, ToolLoader, ToolRegistry

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


class SubagentManager:
    """Run an isolated task context with separately loaded built-in tools."""

    def __init__(
        self,
        runner: AgentRunner,
        provider: LLMProvider,
        context_builder: ContextBuilder,
        tool_context: ToolContext,
        tool_loader: ToolLoader | None = None,
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

        self._runner = runner
        self._provider = provider
        self._context_builder = context_builder
        self._tool_registry = ToolRegistry()
        (tool_loader or ToolLoader()).load(self._tool_registry, tool_context)

    async def run(self, task: str) -> SubagentRunResult:
        """Run one task without parent history, sessions, or message routing."""

        if not isinstance(task, str) or not task.strip():
            raise ValueError("Subagent task must be a non-empty string")

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
