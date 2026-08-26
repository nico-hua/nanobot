"""A built-in tool for exact UTF-8 text replacement in workspace files."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from ..base import Tool, ToolParameter, ToolResult
from ..context import ToolContext
from ._workspace import resolve_workspace, resolve_workspace_path, tool_error


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
