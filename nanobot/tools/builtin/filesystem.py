"""Built-in tools for safely working with UTF-8 workspace files."""

from __future__ import annotations

import asyncio
import fnmatch
import re
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PureWindowsPath

from ..base import Tool, ToolParameter, ToolResult
from ..context import ToolContext
from ._workspace import resolve_workspace, resolve_workspace_path, tool_error

_SKIPPED_SEARCH_DIRECTORY_NAMES = frozenset({".git", "node_modules", "dist", ".vite"})
_LINE_TRUNCATION_SUFFIX = " … [line truncated]"
_OUTPUT_TRUNCATION_SUFFIX = "... [output truncated]"


@dataclass(frozen=True)
class _PatchHunk:
    """One exact line-level replacement in an update-file patch section."""

    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]
    added_lines: int
    removed_lines: int


@dataclass(frozen=True)
class _PatchOperation:
    """One parsed add, update, or delete operation from an input patch."""

    kind: str
    path: str
    hunks: tuple[_PatchHunk, ...] = ()
    added_file_lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class _PreparedPatchChange:
    """A fully validated patch operation ready to be committed to disk."""

    kind: str
    path: str
    target: Path
    content: str | None
    original_exists: bool
    added_lines: int
    removed_lines: int


@dataclass
class _StagedPatchChange:
    """Temporary files and rollback state for one prepared change."""

    change: _PreparedPatchChange
    staged_path: Path | None = None
    backup_path: Path | None = None
    original_moved: bool = False
    replacement_written: bool = False


class _PatchError(Exception):
    """Expected validation error that should become a tool error result."""


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


class ApplyPatchTool(Tool):
    """Apply a strict, workspace-contained line patch without shell commands."""

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = resolve_workspace(workspace, "ApplyPatchTool")
        super().__init__(
            name="apply_patch",
            description=(
                "Apply a strict line patch inside the workspace. The patch must start "
                "with '*** Begin Patch' and end with '*** End Patch'. Use '*** Add "
                "File: relative/path', '*** Update File: relative/path' with @@ hunks "
                "whose lines start with space, +, or -, or '*** Delete File: "
                "relative/path'."
            ),
            parameters=(
                ToolParameter(
                    name="patch",
                    description="The complete structured patch to validate and apply.",
                    type="string",
                    required=True,
                ),
            ),
        )

    @classmethod
    def enabled(cls, context: ToolContext) -> bool:
        return context.workspace is not None

    @classmethod
    def create(cls, context: ToolContext) -> ApplyPatchTool:
        if context.workspace is None:
            raise ValueError("ApplyPatchTool requires a workspace")
        return cls(context.workspace)

    async def execute(self, patch: str) -> ToolResult:
        """Validate every operation before atomically committing the patch set."""

        if not isinstance(patch, str) or not patch.strip():
            return tool_error("Patch must be a non-empty string")

        try:
            operations = _parse_patch(patch)
        except _PatchError as exc:
            return tool_error(str(exc))

        try:
            changes = await asyncio.to_thread(
                _prepare_patch_changes,
                self.workspace,
                operations,
            )
        except _PatchError as exc:
            return tool_error(str(exc))
        except (OSError, UnicodeError, RuntimeError) as exc:
            detail = getattr(exc, "strerror", None) or str(exc)
            return tool_error(f"Unable to prepare patch: {detail}")

        try:
            await asyncio.to_thread(
                _apply_prepared_patch_changes,
                self.workspace,
                changes,
            )
        except (OSError, UnicodeError, RuntimeError) as exc:
            detail = getattr(exc, "strerror", None) or str(exc)
            return tool_error(f"Unable to apply patch: {detail}")

        return ToolResult(content=_format_patch_summary(changes))


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


def _parse_patch(patch: str) -> tuple[_PatchOperation, ...]:
    """Parse the small structured patch format accepted by ``ApplyPatchTool``."""

    lines = patch.splitlines()
    if not lines or lines[0] != "*** Begin Patch":
        raise _PatchError("Patch must start with '*** Begin Patch'")
    if len(lines) < 2 or lines[-1] != "*** End Patch":
        raise _PatchError("Patch must end with '*** End Patch'")

    operations: list[_PatchOperation] = []
    index = 1
    end_index = len(lines) - 1
    while index < end_index:
        header = lines[index]
        kind, path = _parse_patch_header(header, index + 1)
        index += 1

        body_start = index
        while index < end_index and not lines[index].startswith("*** "):
            index += 1
        body = lines[body_start:index]

        if kind == "add":
            added_lines = _parse_added_file_lines(body, body_start + 1)
            operations.append(
                _PatchOperation(
                    kind=kind,
                    path=path,
                    added_file_lines=added_lines,
                )
            )
        elif kind == "update":
            operations.append(
                _PatchOperation(
                    kind=kind,
                    path=path,
                    hunks=_parse_update_hunks(body, body_start + 1),
                )
            )
        else:
            if body:
                raise _PatchError(
                    f"Delete File section must not contain body lines (line {body_start + 1})"
                )
            operations.append(_PatchOperation(kind=kind, path=path))

    if not operations:
        raise _PatchError("Patch must contain at least one file operation")
    return tuple(operations)


def _parse_patch_header(header: str, line_number: int) -> tuple[str, str]:
    for prefix, kind in (
        ("*** Add File: ", "add"),
        ("*** Update File: ", "update"),
        ("*** Delete File: ", "delete"),
    ):
        if header.startswith(prefix):
            path = header.removeprefix(prefix).strip()
            if not path:
                raise _PatchError(f"Patch file path is empty at line {line_number}")
            return kind, path
    raise _PatchError(f"Invalid patch file operation at line {line_number}")


def _parse_added_file_lines(lines: list[str], start_line: int) -> tuple[str, ...]:
    added_lines: list[str] = []
    for offset, line in enumerate(lines):
        if not line.startswith("+"):
            raise _PatchError(
                f"Add File content must start with '+' (line {start_line + offset})"
            )
        added_lines.append(line[1:])
    return tuple(added_lines)


def _parse_update_hunks(lines: list[str], start_line: int) -> tuple[_PatchHunk, ...]:
    if not lines:
        raise _PatchError(f"Update File section requires at least one hunk (line {start_line})")

    hunks: list[_PatchHunk] = []
    index = 0
    while index < len(lines):
        line_number = start_line + index
        if lines[index] != "@@":
            raise _PatchError(f"Expected '@@' hunk marker at line {line_number}")
        index += 1

        old_lines: list[str] = []
        new_lines: list[str] = []
        added_lines = 0
        removed_lines = 0
        while index < len(lines) and lines[index] != "@@":
            line = lines[index]
            current_line_number = start_line + index
            if not line or line[0] not in {" ", "+", "-"}:
                raise _PatchError(
                    "Patch hunk lines must start with space, '+', or '-' "
                    f"(line {current_line_number})"
                )

            content = line[1:]
            if line[0] == " ":
                old_lines.append(content)
                new_lines.append(content)
            elif line[0] == "+":
                new_lines.append(content)
                added_lines += 1
            else:
                old_lines.append(content)
                removed_lines += 1
            index += 1

        if not old_lines:
            raise _PatchError(
                "Patch hunk must include at least one context or removed line "
                f"(line {line_number})"
            )
        hunks.append(
            _PatchHunk(
                old_lines=tuple(old_lines),
                new_lines=tuple(new_lines),
                added_lines=added_lines,
                removed_lines=removed_lines,
            )
        )

    return tuple(hunks)


def _prepare_patch_changes(
    workspace: Path,
    operations: tuple[_PatchOperation, ...],
) -> tuple[_PreparedPatchChange, ...]:
    """Read, validate, and calculate every change before any write occurs."""

    changes: list[_PreparedPatchChange] = []
    changed_paths: set[Path] = set()
    for operation in operations:
        target = _resolve_patch_path(workspace, operation.path)
        if target in changed_paths:
            raise _PatchError(
                f"Patch modifies the same file more than once: {operation.path}"
            )
        changed_paths.add(target)
        relative_path = target.relative_to(workspace).as_posix()

        if operation.kind == "add":
            if target.exists():
                raise _PatchError(f"Cannot add an existing file: {relative_path}")
            content = _join_added_file_lines(operation.added_file_lines)
            changes.append(
                _PreparedPatchChange(
                    kind=operation.kind,
                    path=relative_path,
                    target=target,
                    content=content,
                    original_exists=False,
                    added_lines=len(operation.added_file_lines),
                    removed_lines=0,
                )
            )
            continue

        if not target.exists():
            raise _PatchError(f"File not found: {relative_path}")
        if not target.is_file():
            raise _PatchError(f"Path is not a file: {relative_path}")
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise _PatchError(f"File is not valid UTF-8 text: {relative_path}") from None
        except OSError as exc:
            detail = exc.strerror or str(exc)
            raise _PatchError(
                f"Unable to read file: {relative_path} ({detail})"
            ) from exc

        if operation.kind == "delete":
            changes.append(
                _PreparedPatchChange(
                    kind=operation.kind,
                    path=relative_path,
                    target=target,
                    content=None,
                    original_exists=True,
                    added_lines=0,
                    removed_lines=len(content.splitlines()),
                )
            )
            continue

        updated_content = _apply_patch_hunks(relative_path, content, operation.hunks)
        changes.append(
            _PreparedPatchChange(
                kind=operation.kind,
                path=relative_path,
                target=target,
                content=updated_content,
                original_exists=True,
                added_lines=sum(hunk.added_lines for hunk in operation.hunks),
                removed_lines=sum(hunk.removed_lines for hunk in operation.hunks),
            )
        )

    return tuple(changes)


def _resolve_patch_path(workspace: Path, path: str) -> Path:
    """Resolve a non-symlink, strictly relative patch target inside workspace."""

    if not isinstance(path, str) or not path.strip():
        raise _PatchError("Patch file path must be a non-empty string")
    if _is_absolute_path(path):
        raise _PatchError("Patch file path must be relative to the workspace")
    if _has_parent_segment(path):
        raise _PatchError("Patch file path must not contain '..'")

    current = workspace
    try:
        for part in Path(path).parts:
            if part == ".":
                continue
            current = current / part
            if current.is_symlink():
                raise _PatchError(
                    f"Patch file path must not use symbolic links: {path}"
                )
    except OSError as exc:
        detail = exc.strerror or str(exc)
        raise _PatchError(f"Unable to resolve patch file path: {detail}") from exc

    resolved = resolve_workspace_path(workspace, path)
    if isinstance(resolved, ToolResult):
        raise _PatchError(resolved.error or "Unable to resolve patch file path")
    return resolved


def _apply_patch_hunks(
    path: str,
    content: str,
    hunks: tuple[_PatchHunk, ...],
) -> str:
    """Apply each uniquely matched hunk to in-memory text content."""

    lines = content.splitlines()
    line_ending = "\r\n" if "\r\n" in content else "\n"
    trailing_newline = content.endswith(("\n", "\r"))

    for hunk in hunks:
        matches = _find_hunk_positions(lines, hunk.old_lines)
        if not matches:
            raise _PatchError(f"Patch context not found in file: {path}")
        if len(matches) > 1:
            raise _PatchError(f"Patch context is ambiguous in file: {path}")

        start = matches[0]
        lines[start : start + len(hunk.old_lines)] = hunk.new_lines

    content = line_ending.join(lines)
    return f"{content}{line_ending}" if trailing_newline and lines else content


def _find_hunk_positions(lines: list[str], old_lines: tuple[str, ...]) -> list[int]:
    """Find exact line-sequence matches without any fuzzy matching."""

    length = len(old_lines)
    return [
        index
        for index in range(len(lines) - length + 1)
        if tuple(lines[index : index + length]) == old_lines
    ]


def _join_added_file_lines(lines: tuple[str, ...]) -> str:
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _apply_prepared_patch_changes(
    workspace: Path,
    changes: tuple[_PreparedPatchChange, ...],
) -> None:
    """Stage all content, then replace targets with rollback on write failure."""

    staged_changes: list[_StagedPatchChange] = []
    created_directories: list[Path] = []
    committed = False
    try:
        for change in changes:
            staged_change = _StagedPatchChange(change=change)
            staged_changes.append(staged_change)
            if change.content is None:
                continue

            created_directories.extend(
                _create_missing_patch_directories(workspace, change.target.parent)
            )
            staged_change.staged_path = _stage_utf8_file(
                change.target.parent,
                change.content,
            )

        for staged_change in staged_changes:
            change = staged_change.change
            if change.original_exists:
                staged_change.backup_path = _reserve_temporary_path(change.target.parent)
                change.target.replace(staged_change.backup_path)
                staged_change.original_moved = True
            if staged_change.staged_path is not None:
                staged_change.staged_path.replace(change.target)
                staged_change.replacement_written = True

        committed = True
    except Exception:
        _rollback_patch_changes(staged_changes)
        _remove_empty_patch_directories(created_directories)
        raise
    finally:
        _cleanup_staged_paths(staged_changes)
        if committed:
            _cleanup_backup_paths(staged_changes)
        else:
            _cleanup_unmoved_backup_paths(staged_changes)


def _create_missing_patch_directories(workspace: Path, parent: Path) -> list[Path]:
    """Create add-file parents while recording directories for rollback cleanup."""

    missing_directories: list[Path] = []
    current = parent
    while current != workspace and not current.exists():
        missing_directories.append(current)
        current = current.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        _remove_empty_patch_directories(missing_directories)
        raise
    return missing_directories


def _stage_utf8_file(directory: Path, content: str) -> Path:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
        if temporary_path is None:
            raise RuntimeError("Unable to create patch temporary file")
        return temporary_path
    except Exception:
        _unlink_if_exists(temporary_path)
        raise


def _reserve_temporary_path(directory: Path) -> Path:
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as temporary_file:
        return Path(temporary_file.name)


def _rollback_patch_changes(staged_changes: list[_StagedPatchChange]) -> None:
    """Best-effort restoration of originals after a failed commit."""

    for staged_change in reversed(staged_changes):
        try:
            target = staged_change.change.target
            if staged_change.original_moved:
                if target.exists() or target.is_symlink():
                    target.unlink()
                if (
                    staged_change.backup_path is not None
                    and staged_change.backup_path.exists()
                ):
                    staged_change.backup_path.replace(target)
            elif staged_change.replacement_written and (
                target.exists() or target.is_symlink()
            ):
                target.unlink()
        except OSError:
            # Preserve the original exception and any remaining backup rather
            # than masking the failed commit with a cleanup error.
            continue


def _remove_empty_patch_directories(directories: list[Path]) -> None:
    for directory in sorted(
        set(directories),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            continue


def _cleanup_staged_paths(staged_changes: list[_StagedPatchChange]) -> None:
    for staged_change in staged_changes:
        _unlink_if_exists(staged_change.staged_path)


def _cleanup_backup_paths(staged_changes: list[_StagedPatchChange]) -> None:
    for staged_change in staged_changes:
        _unlink_if_exists(staged_change.backup_path)


def _cleanup_unmoved_backup_paths(staged_changes: list[_StagedPatchChange]) -> None:
    for staged_change in staged_changes:
        if not staged_change.original_moved:
            _unlink_if_exists(staged_change.backup_path)


def _unlink_if_exists(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return


def _format_patch_summary(changes: tuple[_PreparedPatchChange, ...]) -> str:
    """Return a concise, user-visible list of committed file changes."""

    lines = ["Applied patch:"]
    for change in changes:
        if change.kind == "add":
            lines.append(f"- added {change.path} (+{change.added_lines})")
        elif change.kind == "delete":
            lines.append(f"- deleted {change.path} (-{change.removed_lines})")
        else:
            lines.append(
                f"- updated {change.path} (+{change.added_lines}/-{change.removed_lines})"
            )
    return "\n".join(lines)


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


class FindFilesTool(Tool):
    """Find workspace files by a filename or glob pattern without reading them."""

    DEFAULT_LIMIT = 200
    MAX_LIMIT = 1000

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = resolve_workspace(workspace, "FindFilesTool")
        super().__init__(
            name="find_files",
            description=(
                "Find files by name or glob pattern inside the workspace without "
                "reading their contents."
            ),
            parameters=(
                ToolParameter(
                    name="pattern",
                    description=(
                        "A workspace-relative filename glob, such as *.py or **/*.py."
                    ),
                    type="string",
                    required=True,
                ),
                ToolParameter(
                    name="path",
                    description=(
                        "An optional workspace-relative directory to search; defaults "
                        "to the workspace root."
                    ),
                    type="string",
                ),
                ToolParameter(
                    name="limit",
                    description="The maximum number of files to return, up to 1000.",
                    type="integer",
                ),
            ),
        )

    @classmethod
    def enabled(cls, context: ToolContext) -> bool:
        return context.workspace is not None

    @classmethod
    def create(cls, context: ToolContext) -> FindFilesTool:
        if context.workspace is None:
            raise ValueError("FindFilesTool requires a workspace")
        return cls(context.workspace)

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        limit: int | None = None,
    ) -> ToolResult:
        """Return stable workspace-relative paths matching one safe glob pattern."""

        pattern_parts = _glob_pattern_parts(pattern)
        if isinstance(pattern_parts, ToolResult):
            return pattern_parts
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

        target = _resolve_search_directory(self.workspace, path)
        if isinstance(target, ToolResult):
            return target
        if not target.exists():
            return tool_error(f"Directory not found: {path}")
        if not target.is_dir():
            return tool_error(f"Path is not a directory: {path}")

        try:
            matches = await asyncio.to_thread(
                _find_matching_files,
                self.workspace,
                target,
                pattern_parts,
                limit,
            )
        except (OSError, RuntimeError) as exc:
            detail = getattr(exc, "strerror", None) or str(exc)
            return tool_error(f"Unable to search files: {path} ({detail})")

        return ToolResult(content="\n".join(matches) or "(no files found)")


def _resolve_search_directory(workspace: Path, path: str) -> Path | ToolResult:
    """Resolve a strictly workspace-relative directory for file search."""

    if not isinstance(path, str) or not path.strip():
        return tool_error("Search path must be a non-empty string")
    if _is_absolute_path(path):
        return tool_error("Search path must be relative to the workspace")
    if _has_parent_segment(path):
        return tool_error("Search path must not contain '..'")
    return resolve_workspace_path(workspace, path)


def _glob_pattern_parts(
    pattern: str,
    *,
    label: str = "Pattern",
) -> tuple[str, ...] | ToolResult:
    """Validate and normalize a workspace-relative glob into path segments."""

    if not isinstance(pattern, str) or not pattern.strip():
        return tool_error(f"{label} must be a non-empty string")
    if _is_absolute_path(pattern):
        return tool_error(f"{label} must be relative to the workspace")
    if _has_parent_segment(pattern):
        return tool_error(f"{label} must not contain '..'")

    parts = tuple(
        part
        for part in pattern.strip().replace("\\", "/").split("/")
        if part and part != "."
    )
    if not parts:
        return tool_error(f"{label} must include a filename glob")
    return parts


def _is_absolute_path(value: str) -> bool:
    """Recognize both host-native and Windows drive-qualified absolute paths."""

    windows_path = PureWindowsPath(value)
    return (
        Path(value).is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or value.startswith(("/", "\\"))
    )


def _has_parent_segment(value: str) -> bool:
    return ".." in value.replace("\\", "/").split("/")


def _find_matching_files(
    workspace: Path,
    directory: Path,
    pattern_parts: tuple[str, ...],
    limit: int,
) -> list[str]:
    """Walk a directory without following links or generated directories."""

    return [
        file.relative_to(workspace).as_posix()
        for file in _workspace_files(workspace, directory)
        if _matches_glob(file.relative_to(directory).as_posix(), pattern_parts)
    ][:limit]


def _workspace_files(workspace: Path, directory: Path) -> list[Path]:
    """Return all regular, searchable workspace files in stable order."""

    files: list[Path] = []

    def visit(current: Path) -> None:
        children = sorted(
            current.iterdir(),
            key=lambda child: (child.name.casefold(), child.name),
        )
        for child in children:
            # A link can target any location or change between checks. Never
            # traverse or report it as a searchable workspace file.
            if child.is_symlink():
                continue

            resolved_child = child.resolve()
            if not resolved_child.is_relative_to(workspace):
                continue
            if child.is_dir():
                if child.name.casefold() not in _SKIPPED_SEARCH_DIRECTORY_NAMES:
                    visit(child)
                continue
            if child.is_file():
                files.append(child)

    visit(directory)
    return sorted(
        files,
        key=lambda file: (
            file.relative_to(workspace).as_posix().casefold(),
            file.relative_to(workspace).as_posix(),
        ),
    )


def _matches_glob(path: str, pattern_parts: tuple[str, ...]) -> bool:
    """Match slash-separated glob segments, with ** spanning directories."""

    path_parts = tuple(part for part in path.split("/") if part)

    @cache
    def matches(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)

        current_pattern = pattern_parts[pattern_index]
        if current_pattern == "**":
            return matches(path_index, pattern_index + 1) or (
                path_index < len(path_parts)
                and matches(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[path_index], current_pattern)
            and matches(path_index + 1, pattern_index + 1)
        )

    return matches(0, 0)


class GrepTool(Tool):
    """Search UTF-8 workspace files line by line with a regular expression."""

    DEFAULT_LIMIT = 100
    MAX_LIMIT = 1000
    MAX_LINE_CHARACTERS = 500
    MAX_OUTPUT_CHARACTERS = 20_000

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = resolve_workspace(workspace, "GrepTool")
        super().__init__(
            name="grep",
            description=(
                "Search UTF-8 text files in the workspace with a regular expression "
                "and return matching lines."
            ),
            parameters=(
                ToolParameter(
                    name="pattern",
                    description="A regular expression to match against individual lines.",
                    type="string",
                    required=True,
                ),
                ToolParameter(
                    name="path",
                    description=(
                        "An optional workspace-relative directory to search; defaults "
                        "to the workspace root."
                    ),
                    type="string",
                ),
                ToolParameter(
                    name="include",
                    description=(
                        "An optional filename glob filter, such as *.py. A filename-only "
                        "glob applies in all searched directories."
                    ),
                    type="string",
                ),
                ToolParameter(
                    name="case_sensitive",
                    description="Whether matches are case-sensitive; defaults to true.",
                    type="boolean",
                ),
                ToolParameter(
                    name="limit",
                    description="The maximum number of matching lines to return, up to 1000.",
                    type="integer",
                ),
            ),
        )

    @classmethod
    def enabled(cls, context: ToolContext) -> bool:
        return context.workspace is not None

    @classmethod
    def create(cls, context: ToolContext) -> GrepTool:
        if context.workspace is None:
            raise ValueError("GrepTool requires a workspace")
        return cls(context.workspace)

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        include: str | None = None,
        case_sensitive: bool = True,
        limit: int | None = None,
    ) -> ToolResult:
        """Search safely scoped text files and return bounded matching lines."""

        if not isinstance(pattern, str) or not pattern:
            return tool_error("Pattern must be a non-empty regular expression")
        if not isinstance(case_sensitive, bool):
            return tool_error("Case sensitivity must be a boolean")
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

        try:
            expression = re.compile(
                pattern,
                0 if case_sensitive else re.IGNORECASE,
            )
        except re.error as exc:
            return tool_error(f"Invalid regular expression: {exc}")

        include_parts: tuple[str, ...] | None = None
        if include is not None:
            include_parts = _glob_pattern_parts(include, label="Include pattern")
            if isinstance(include_parts, ToolResult):
                return include_parts

        target = _resolve_search_directory(self.workspace, path)
        if isinstance(target, ToolResult):
            return target
        if not target.exists():
            return tool_error(f"Directory not found: {path}")
        if not target.is_dir():
            return tool_error(f"Path is not a directory: {path}")

        try:
            matches, output_truncated = await asyncio.to_thread(
                _grep_workspace_files,
                self.workspace,
                target,
                expression,
                include_parts,
                limit,
                self.MAX_LINE_CHARACTERS,
                self.MAX_OUTPUT_CHARACTERS,
            )
        except (OSError, RuntimeError) as exc:
            detail = getattr(exc, "strerror", None) or str(exc)
            return tool_error(f"Unable to search files: {path} ({detail})")

        return ToolResult(content=_format_grep_result(matches, output_truncated))


def _grep_workspace_files(
    workspace: Path,
    directory: Path,
    expression: re.Pattern[str],
    include_parts: tuple[str, ...] | None,
    limit: int,
    max_line_characters: int,
    max_output_characters: int,
) -> tuple[list[str], bool]:
    """Search sorted workspace files and return bounded formatted line matches."""

    matches: list[str] = []
    output_length = 0

    for file in _workspace_files(workspace, directory):
        relative_to_directory = file.relative_to(directory).as_posix()
        if include_parts is not None and not _matches_include(
            relative_to_directory,
            include_parts,
        ):
            continue

        relative_to_workspace = file.relative_to(workspace).as_posix()
        for line_number, line in _matching_lines(file, expression):
            formatted_line = _truncate_matched_line(line, max_line_characters)
            match = f"{relative_to_workspace}:{line_number}: {formatted_line}"
            separator_length = 1 if matches else 0
            # Reserve space for an explicit notice if the next matching line
            # would cross the total output budget.
            reserved_notice_length = len(_OUTPUT_TRUNCATION_SUFFIX) + (
                1 if matches else 0
            )
            if (
                output_length
                + separator_length
                + len(match)
                + reserved_notice_length
                > max_output_characters
            ):
                return matches, True

            matches.append(match)
            output_length += separator_length + len(match)
            if len(matches) >= limit:
                return matches, False

    return matches, False


def _matching_lines(
    path: Path,
    expression: re.Pattern[str],
) -> Iterator[tuple[int, str]]:
    """Yield matches from one UTF-8 text file, skipping binary-like files."""

    try:
        with path.open("r", encoding="utf-8") as file:
            for line_number, line in enumerate(file, start=1):
                # Null bytes identify a binary-like stream even when its bytes
                # happen to be valid UTF-8, so stop scanning that file.
                if "\x00" in line:
                    return
                if expression.search(line) is not None:
                    yield line_number, line
    except UnicodeDecodeError:
        return


def _matches_include(path: str, include_parts: tuple[str, ...]) -> bool:
    """Match a filename-only include globally, or a path-aware include exactly."""

    if len(include_parts) == 1:
        return fnmatch.fnmatchcase(path.rsplit("/", maxsplit=1)[-1], include_parts[0])
    return _matches_glob(path, include_parts)


def _truncate_matched_line(line: str, max_line_characters: int) -> str:
    """Remove line endings and bound one displayed match line."""

    content = line.rstrip("\r\n")
    if len(content) <= max_line_characters:
        return content
    if max_line_characters <= len(_LINE_TRUNCATION_SUFFIX):
        return content[:max_line_characters]
    prefix_length = max_line_characters - len(_LINE_TRUNCATION_SUFFIX)
    return f"{content[:prefix_length]}{_LINE_TRUNCATION_SUFFIX}"


def _format_grep_result(matches: list[str], output_truncated: bool) -> str:
    if not matches:
        return _OUTPUT_TRUNCATION_SUFFIX if output_truncated else "(no matches)"
    content = "\n".join(matches)
    if output_truncated:
        return f"{content}\n{_OUTPUT_TRUNCATION_SUFFIX}"
    return content
