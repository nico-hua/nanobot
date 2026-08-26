"""A built-in tool for reading UTF-8 text files from a workspace."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..base import Tool, ToolParameter, ToolResult
from ._workspace import resolve_workspace, resolve_workspace_path, tool_error


class ReadFileTool(Tool):
    """Read a range of lines from a text file inside one workspace."""

    DEFAULT_LIMIT = 200
    MAX_LIMIT = 1000

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = resolve_workspace(workspace, "ReadFileTool")

        super().__init__(
            name="read_file",
            description="Read UTF-8 text from a file in the workspace.",
            parameters=(
                ToolParameter(
                    name="path",
                    description="A path to a file inside the workspace.",
                    type="string",
                    required=True,
                ),
                ToolParameter(
                    name="offset",
                    description="The zero-based line offset to start reading from.",
                    type="integer",
                ),
                ToolParameter(
                    name="limit",
                    description="The maximum number of lines to return, up to 1000.",
                    type="integer",
                ),
            ),
        )

    async def execute(
        self,
        path: str,
        offset: int = 0,
        limit: int | None = None,
    ) -> ToolResult:
        """Read UTF-8 lines from a workspace-relative or contained absolute path."""

        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            return tool_error("Offset must be a non-negative integer")
        if limit is None:
            limit = self.DEFAULT_LIMIT
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit <= 0
            or limit > self.MAX_LIMIT
        ):
            return tool_error(
                f"Limit must be an integer between 1 and {self.MAX_LIMIT}"
            )

        target = resolve_workspace_path(self.workspace, path)
        if isinstance(target, ToolResult):
            return target
        if not target.exists():
            return tool_error(f"File not found: {path}")
        if not target.is_file():
            return tool_error(f"Path is not a file: {path}")

        try:
            content = await asyncio.to_thread(target.read_text, encoding="utf-8")
        except UnicodeDecodeError:
            return tool_error(f"File is not valid UTF-8 text: {path}")
        except OSError as exc:
            detail = exc.strerror or str(exc)
            return tool_error(f"Unable to read file: {path} ({detail})")

        lines = content.splitlines()
        return ToolResult(content="\n".join(lines[offset : offset + limit]))
