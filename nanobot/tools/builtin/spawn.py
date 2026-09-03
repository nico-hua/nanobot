"""A built-in tool for synchronously running one isolated subagent task."""

from __future__ import annotations

import asyncio
import logging

from ...subagent import SubagentManager
from ..base import Tool, ToolParameter, ToolResult
from ..context import ToolContext, get_request_context

logger = logging.getLogger(__name__)


class SpawnTool(Tool):
    """Run a focused subagent task and return its final text to the main Agent."""

    def __init__(self, subagent_manager: SubagentManager) -> None:
        if not isinstance(subagent_manager, SubagentManager):
            raise TypeError("SpawnTool requires a SubagentManager")
        self._subagent_manager = subagent_manager
        super().__init__(
            name="spawn",
            description="Run a focused subagent task and return its final result.",
            parameters=(
                ToolParameter(
                    name="task",
                    description="The focused task for the subagent to complete.",
                    type="string",
                    required=True,
                ),
            ),
        )

    @classmethod
    def enabled(cls, context: ToolContext) -> bool:
        return context.subagent_manager is not None

    @classmethod
    def create(cls, context: ToolContext) -> SpawnTool:
        if context.subagent_manager is None:
            raise ValueError("SpawnTool requires a SubagentManager")
        return cls(context.subagent_manager)

    async def execute(self, task: str) -> ToolResult:
        """Synchronously run the child task inside the current request context."""

        if not isinstance(task, str) or not task.strip():
            return _tool_error("Task must be a non-empty string")
        request_context = get_request_context()
        if request_context is None:
            return _tool_error("Spawn is only available while processing a user message")

        try:
            result = await self._subagent_manager.run(
                task.strip(),
                request_context=request_context,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception("Subagent spawn failed")
            return _tool_error(f"Subagent execution failed: {type(error).__name__}")

        if not result.success or result.agent_result is None:
            return _tool_error(result.error or "Subagent could not complete the task")
        return ToolResult(content=result.agent_result.content)


def _tool_error(message: str) -> ToolResult:
    return ToolResult(content=f"Error: {message}", success=False, error=message)
