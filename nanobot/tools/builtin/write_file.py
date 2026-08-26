"""A built-in tool for writing UTF-8 text files in a workspace."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..base import Tool, ToolParameter, ToolResult
from ._workspace import resolve_workspace, resolve_workspace_path, tool_error


class WriteFileTool(Tool):
    """Create or fully overwrite a text file inside one workspace."""

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = resolve_workspace(workspace, "WriteFileTool")

        super().__init__(
            name="write_file",
            description="Create or fully overwrite a UTF-8 text file in the workspace.",
            parameters=(
                ToolParameter(
                    name="path",
                    description="A path to a file inside the workspace.",
                    type="string",
                    required=True,
                ),
                ToolParameter(
                    name="content",
                    description="The complete UTF-8 text content to write.",
                    type="string",
                    required=True,
                ),
            ),
        )

    async def execute(self, path: str, content: str) -> ToolResult:
        """Create parent directories and write all text to a workspace file."""

        if not isinstance(content, str):
            return tool_error("File content must be a string")

        target = resolve_workspace_path(self.workspace, path)
        if isinstance(target, ToolResult):
            return target

        try:
            await asyncio.to_thread(_write_utf8_file, target, content)
        except (OSError, UnicodeError) as exc:
            detail = getattr(exc, "strerror", None) or str(exc)
            return tool_error(f"Unable to write file: {path} ({detail})")

        return ToolResult(content=f"Wrote {len(content)} characters to {path}")


def _write_utf8_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
