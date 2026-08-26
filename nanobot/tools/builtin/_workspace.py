"""Shared workspace path handling for built-in file tools."""

from __future__ import annotations

from pathlib import Path

from ..base import ToolResult


def resolve_workspace(workspace: str | Path, tool_name: str) -> Path:
    """Resolve and validate a workspace directory for one tool instance."""

    resolved_workspace = Path(workspace).resolve()
    if not resolved_workspace.is_dir():
        raise ValueError(f"{tool_name} workspace must be an existing directory")
    return resolved_workspace


def resolve_workspace_path(workspace: Path, path: str) -> Path | ToolResult:
    """Resolve a path and ensure it remains inside the workspace."""

    if not isinstance(path, str) or not path.strip():
        return tool_error("File path must be a non-empty string")

    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = workspace / candidate

    try:
        target = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        detail = getattr(exc, "strerror", None) or str(exc)
        return tool_error(f"Unable to resolve file path: {path} ({detail})")

    if not target.is_relative_to(workspace):
        return tool_error(f"Path is outside the workspace: {path}")
    return target


def tool_error(message: str) -> ToolResult:
    """Return a failed tool result using the built-in tool convention."""

    return ToolResult(content=f"Error: {message}", success=False, error=message)
