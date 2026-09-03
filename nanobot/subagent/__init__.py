"""Isolated one-shot subagent execution primitives."""

from .manager import BackgroundSubagentTask, SubagentManager, SubagentRunResult

__all__ = [
    "BackgroundSubagentTask",
    "SubagentManager",
    "SubagentRunResult",
]
