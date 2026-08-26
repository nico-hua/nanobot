import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from nanobot.tools import Tool
from nanobot.tools.builtin import ExecTool


def python_command(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


class ExecToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self._temporary_directory.name)
        self.tool = ExecTool(self.workspace)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_executes_in_workspace_and_returns_stdout(self) -> None:
        result = await self.tool.execute(
            python_command("import os; print('hello'); print(os.getcwd())")
        )

        self.assertIsInstance(self.tool, Tool)
        self.assertTrue(result.success)
        self.assertIn("Exit code: 0", result.content)
        self.assertIn("stdout:", result.content)
        self.assertIn("hello", result.content)
        self.assertIn(str(self.workspace), result.content)
        self.assertIn("stderr:\n(empty)", result.content)

    async def test_preserves_stderr_and_non_zero_exit_code(self) -> None:
        result = await self.tool.execute(
            python_command(
                "import sys; sys.stderr.write('warning\\n'); sys.exit(3)"
            )
        )

        self.assertTrue(result.success)
        self.assertIn("Exit code: 3", result.content)
        self.assertIn("stderr:\nwarning", result.content)

    async def test_uses_a_workspace_contained_working_directory(self) -> None:
        working_directory = self.workspace / "nested"
        working_directory.mkdir()

        result = await self.tool.execute(
            python_command("import os; print(os.getcwd())"),
            working_dir="nested",
        )

        self.assertTrue(result.success)
        self.assertIn(str(working_directory), result.content)

    async def test_rejects_working_directories_outside_the_workspace(self) -> None:
        result = await self.tool.execute(
            python_command("print('should not run')"),
            working_dir="../outside",
        )

        self.assertFalse(result.success)
        self.assertEqual(
            result.error,
            "Path is outside the workspace: ../outside",
        )

    async def test_does_not_inherit_arbitrary_parent_environment_variables(self) -> None:
        with patch.dict(
            os.environ,
            {"NANOBOT_EXEC_TEST_SECRET": "not-for-child-process"},
        ):
            result = await self.tool.execute(
                python_command(
                    "import os; print(os.getenv('NANOBOT_EXEC_TEST_SECRET', 'missing'))"
                )
            )

        self.assertTrue(result.success)
        self.assertIn("missing", result.content)
        self.assertNotIn("not-for-child-process", result.content)

    async def test_timeout_stops_the_command(self) -> None:
        result = await self.tool.execute(
            python_command("import time; time.sleep(2)"),
            timeout=1,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Command timed out after 1 seconds")

    async def test_returns_errors_for_empty_commands_and_start_failures(self) -> None:
        empty_command = await self.tool.execute(" ")
        with patch(
            "asyncio.create_subprocess_shell",
            new=AsyncMock(side_effect=OSError("shell unavailable")),
        ):
            start_failure = await self.tool.execute("echo hello")

        self.assertEqual(empty_command.error, "Command must be a non-empty string")
        self.assertEqual(
            start_failure.error,
            "Unable to start command: shell unavailable",
        )

    async def test_truncates_long_output(self) -> None:
        result = await self.tool.execute(
            python_command(f"print('x' * {self.tool.MAX_OUTPUT_CHARS + 100})")
        )

        self.assertTrue(result.success)
        self.assertIn("... truncated", result.content)
        self.assertLessEqual(
            len(result.content),
            2 * self.tool.MAX_OUTPUT_CHARS + 100,
        )
