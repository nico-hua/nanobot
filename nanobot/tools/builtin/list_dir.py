"""A built-in tool for listing directories in a workspace."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..base import Tool, ToolParameter, ToolResult
from ._workspace import resolve_workspace, resolve_workspace_path, tool_error


class ListDirTool(Tool):
    """List files and directories inside one workspace directory."""

    DEFAULT_LIMIT = 200
    MAX_LIMIT = 1000

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = resolve_workspace(workspace, "ListDirTool")

        super().__init__(
            name="list_dir",
            description="List files and directories inside the workspace.",
            parameters=(
                ToolParameter(
                    name="path",
                    description="A directory path inside the workspace.",
                    type="string",
                ),
                ToolParameter(
                    name="recursive",
                    description="Whether to include nested directory entries.",
                    type="boolean",
                ),
                ToolParameter(
                    name="limit",
                    description="The maximum number of entries to return, up to 1000.",
                    type="integer",
                ),
            ),
        )

    async def execute(
        self,
        path: str = ".",
        recursive: bool = False,
        limit: int | None = None,
    ) -> ToolResult:
        """List stable, workspace-contained directory entries."""

        if not isinstance(recursive, bool):
            return tool_error("Recursive must be a boolean")
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
            return tool_error(f"Directory not found: {path}")
        if not target.is_dir():
            return tool_error(f"Path is not a directory: {path}")

        try:
            entries = await asyncio.to_thread(_list_directory, target, recursive, limit)
        except OSError as exc:
            detail = exc.strerror or str(exc)
            return tool_error(f"Unable to list directory: {path} ({detail})")

        return ToolResult(content="\n".join(entries) or "(empty)")


def _list_directory(path: Path, recursive: bool, limit: int) -> list[str]:
    entries: list[str] = []

    def visit(directory: Path) -> bool:
        children = sorted(
            directory.iterdir(),
            key=lambda child: (child.name.casefold(), child.name),
        )
        for child in children:
            if len(entries) >= limit:
                return True

            if child.is_symlink():
                entry_type = "link"
                is_directory = False
            elif child.is_dir():
                entry_type = "dir"
                is_directory = True
            else:
                entry_type = "file"
                is_directory = False

            relative_path = child.relative_to(path).as_posix()
            entries.append(f"[{entry_type}] {relative_path}")

            if recursive and is_directory and visit(child):
                return True
        return False

    visit(path)
    return entries
