"""Isolated one-shot subagent execution primitives."""

from .manager import (
    BackgroundSubagentTask,
    SubagentManager,
    SubagentRunResult,
    SubagentTaskStatus,
)

__all__ = [
    "BackgroundSubagentTask",
    "SubagentManager",
    "SubagentRunResult",
    "SubagentTaskStatus",
]
