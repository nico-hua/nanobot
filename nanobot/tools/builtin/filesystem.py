"""Built-in tools for safely working with UTF-8 workspace files."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from ..base import Tool, ToolParameter, ToolResult
from ..context import ToolContext
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

    @classmethod
    def enabled(cls, context: ToolContext) -> bool:
        return context.workspace is not None

    @classmethod
    def create(cls, context: ToolContext) -> ReadFileTool:
        if context.workspace is None:
            raise ValueError("ReadFileTool requires a workspace")
        return cls(context.workspace)

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

    @classmethod
    def enabled(cls, context: ToolContext) -> bool:
        return context.workspace is not None

    @classmethod
    def create(cls, context: ToolContext) -> WriteFileTool:
        if context.workspace is None:
            raise ValueError("WriteFileTool requires a workspace")
        return cls(context.workspace)

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


class EditFileTool(Tool):
    """Replace one exact text occurrence in an existing workspace file."""

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = resolve_workspace(workspace, "EditFileTool")

        super().__init__(
            name="edit_file",
            description="Replace one exact text occurrence in a UTF-8 workspace file.",
            parameters=(
                ToolParameter(
                    name="path",
                    description="A path to an existing file inside the workspace.",
                    type="string",
                    required=True,
                ),
                ToolParameter(
                    name="old_text",
                    description="The exact text that must appear exactly once.",
                    type="string",
                    required=True,
                ),
                ToolParameter(
                    name="new_text",
                    description="The text that replaces old_text.",
                    type="string",
                    required=True,
                ),
            ),
        )

    @classmethod
    def enabled(cls, context: ToolContext) -> bool:
        return context.workspace is not None

    @classmethod
    def create(cls, context: ToolContext) -> EditFileTool:
        if context.workspace is None:
            raise ValueError("EditFileTool requires a workspace")
        return cls(context.workspace)

    async def execute(
        self,
        path: str,
        old_text: str,
        new_text: str,
    ) -> ToolResult:
        """Validate one exact match, then atomically replace it."""

        if not isinstance(old_text, str) or not old_text:
            return tool_error("Old text must be a non-empty string")
        if not isinstance(new_text, str):
            return tool_error("New text must be a string")
        if old_text == new_text:
            return tool_error("Old text and new text must differ")

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

        index = content.find(old_text)
        if index == -1:
            return tool_error(f"Text not found in file: {path}")
        if content.find(old_text, index + 1) != -1:
            return tool_error(f"Text must appear exactly once in file: {path}")

        updated_content = content[:index] + new_text + content[index + len(old_text) :]
        try:
            await asyncio.to_thread(_replace_utf8_file, target, updated_content)
        except (OSError, UnicodeError) as exc:
            detail = getattr(exc, "strerror", None) or str(exc)
            return tool_error(f"Unable to save file: {path} ({detail})")

        return ToolResult(content=f"Replaced text in {path}")


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

    @classmethod
    def enabled(cls, context: ToolContext) -> bool:
        return context.workspace is not None

    @classmethod
    def create(cls, context: ToolContext) -> ListDirTool:
        if context.workspace is None:
            raise ValueError("ListDirTool requires a workspace")
        return cls(context.workspace)

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


def _write_utf8_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _replace_utf8_file(path: Path, content: str) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
        temporary_path.replace(path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


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
