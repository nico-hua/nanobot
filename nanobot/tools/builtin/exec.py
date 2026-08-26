"""A built-in tool for one-shot shell command execution."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from pathlib import Path

from ..base import Tool, ToolParameter, ToolResult
from ..context import ToolContext
from ._workspace import resolve_workspace, resolve_workspace_path, tool_error


class ExecTool(Tool):
    """Execute one shell command with the workspace as its working directory."""

    DEFAULT_TIMEOUT_SECONDS = 30
    MAX_TIMEOUT_SECONDS = 120
    MAX_OUTPUT_CHARS = 8_000

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = resolve_workspace(workspace, "ExecTool")

        super().__init__(
            name="exec",
            description="Run one shell command in the workspace and return its output.",
            parameters=(
                ToolParameter(
                    name="command",
                    description="The shell command to execute in the workspace.",
                    type="string",
                    required=True,
                ),
                ToolParameter(
                    name="timeout",
                    description="The timeout in seconds, up to 120 seconds.",
                    type="integer",
                ),
                ToolParameter(
                    name="working_dir",
                    description="An optional directory path inside the workspace.",
                    type="string",
                ),
            ),
        )

    @classmethod
    def enabled(cls, context: ToolContext) -> bool:
        return context.workspace is not None

    @classmethod
    def create(cls, context: ToolContext) -> ExecTool:
        if context.workspace is None:
            raise ValueError("ExecTool requires a workspace")
        return cls(context.workspace)

    async def execute(
        self,
        command: str,
        timeout: int | None = None,
        working_dir: str | None = None,
    ) -> ToolResult:
        """Run one command and return its exit code, stdout, and stderr."""

        if not isinstance(command, str) or not command.strip():
            return tool_error("Command must be a non-empty string")
        if timeout is None:
            timeout = self.DEFAULT_TIMEOUT_SECONDS
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int)
            or timeout <= 0
            or timeout > self.MAX_TIMEOUT_SECONDS
        ):
            return tool_error(
                "Timeout must be an integer between "
                f"1 and {self.MAX_TIMEOUT_SECONDS} seconds"
            )

        cwd = self._resolve_working_dir(working_dir)
        if isinstance(cwd, ToolResult):
            return cwd
        environment = _minimal_environment()

        try:
            process_options: dict[str, int | bool] = {}
            if os.name == "nt":
                process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            elif os.name == "posix":
                process_options["start_new_session"] = True
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **process_options,
            )
        except (OSError, ValueError) as exc:
            detail = getattr(exc, "strerror", None) or str(exc)
            return tool_error(f"Unable to start command: {detail}")

        communication_task = asyncio.create_task(process.communicate())
        try:
            completed, _ = await asyncio.wait(
                (communication_task,),
                timeout=timeout,
            )
            if not completed:
                await _terminate_process(process)
                await _finish_communication(communication_task)
                return tool_error(f"Command timed out after {timeout} seconds")

            stdout, stderr = communication_task.result()
        except asyncio.CancelledError:
            await _terminate_process(process)
            await _finish_communication(communication_task)
            raise
        except OSError as exc:
            await _terminate_process(process)
            await _finish_communication(communication_task)
            detail = exc.strerror or str(exc)
            return tool_error(f"Unable to execute command: {detail}")

        return ToolResult(
            content=_format_result(
                process.returncode,
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
            )
        )

    def _resolve_working_dir(self, working_dir: str | None) -> Path | ToolResult:
        if working_dir is None:
            return self.workspace
        if not isinstance(working_dir, str) or not working_dir.strip():
            return tool_error("Working directory must be a non-empty string")

        target = resolve_workspace_path(self.workspace, working_dir)
        if isinstance(target, ToolResult):
            return target
        if not target.exists():
            return tool_error(f"Working directory not found: {working_dir}")
        if not target.is_dir():
            return tool_error(f"Working directory is not a directory: {working_dir}")
        return target


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Terminate the started shell process and its supported child processes."""

    try:
        if os.name == "nt":
            cleanup_process = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await cleanup_process.wait()
            if cleanup_process.returncode != 0 and process.returncode is None:
                process.kill()
        elif os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()
        await process.wait()
    except (OSError, ProcessLookupError):
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()


def _minimal_environment() -> dict[str, str]:
    """Return only environment variables needed for ordinary shell commands."""

    variable_names = ["PATH", "LANG", "LC_ALL"]
    if os.name == "nt":
        variable_names.extend(("COMSPEC", "PATHEXT", "SystemRoot", "WINDIR"))

    return {
        name: value
        for name in variable_names
        if (value := os.environ.get(name)) is not None
    }


async def _finish_communication(
    communication_task: asyncio.Task[tuple[bytes, bytes]],
) -> None:
    """Wait for a communication task after its process has been terminated."""

    try:
        await communication_task
    except (OSError, ProcessLookupError):
        return


def _format_result(returncode: int | None, stdout: str, stderr: str) -> str:
    """Format a bounded command result without discarding either output stream."""

    return "\n".join(
        (
            f"Exit code: {returncode}",
            "stdout:",
            _truncate_output(stdout) or "(empty)",
            "stderr:",
            _truncate_output(stderr) or "(empty)",
        )
    )


def _truncate_output(output: str) -> str:
    if len(output) <= ExecTool.MAX_OUTPUT_CHARS:
        return output

    omitted_characters = len(output) - ExecTool.MAX_OUTPUT_CHARS
    marker = f"\n... truncated {omitted_characters} characters"
    return output[: ExecTool.MAX_OUTPUT_CHARS - len(marker)] + marker
