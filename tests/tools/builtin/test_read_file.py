import tempfile
import unittest
from pathlib import Path

from nanobot.tools.builtin import ReadFileTool


class ReadFileToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.tool = ReadFileTool(self.workspace)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_reads_a_workspace_relative_utf8_file(self) -> None:
        (self.workspace / "notes.txt").write_text("first\nsecond", encoding="utf-8")

        result = await self.tool.execute(path="notes.txt")

        self.assertTrue(result.success)
        self.assertEqual(result.content, "first\nsecond")
        self.assertIsNone(result.error)

    async def test_reads_a_limited_range_of_lines(self) -> None:
        (self.workspace / "notes.txt").write_text(
            "zero\none\ntwo\nthree",
            encoding="utf-8",
        )

        result = await self.tool.execute(path="notes.txt", offset=1, limit=2)

        self.assertTrue(result.success)
        self.assertEqual(result.content, "one\ntwo")

    async def test_default_limit_prevents_returning_too_many_lines(self) -> None:
        lines = [f"line {index}" for index in range(self.tool.DEFAULT_LIMIT + 1)]
        (self.workspace / "notes.txt").write_text("\n".join(lines), encoding="utf-8")

        result = await self.tool.execute(path="notes.txt")

        self.assertTrue(result.success)
        self.assertEqual(result.content.splitlines(), lines[: self.tool.DEFAULT_LIMIT])

    async def test_rejects_a_path_outside_the_workspace(self) -> None:
        outside_file = self.root / "private.txt"
        outside_file.write_text("private", encoding="utf-8")

        result = await self.tool.execute(path="../private.txt")

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Path is outside the workspace: ../private.txt")

    async def test_returns_an_error_for_a_missing_file(self) -> None:
        result = await self.tool.execute(path="missing.txt")

        self.assertFalse(result.success)
        self.assertEqual(result.error, "File not found: missing.txt")

    async def test_returns_an_error_for_non_utf8_text(self) -> None:
        (self.workspace / "binary.dat").write_bytes(b"\xff\xfe")

        result = await self.tool.execute(path="binary.dat")

        self.assertFalse(result.success)
        self.assertEqual(result.error, "File is not valid UTF-8 text: binary.dat")

    async def test_returns_an_error_for_a_directory(self) -> None:
        (self.workspace / "folder").mkdir()

        result = await self.tool.execute(path="folder")

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Path is not a file: folder")

    async def test_returns_errors_for_invalid_pagination_arguments(self) -> None:
        (self.workspace / "notes.txt").write_text("content", encoding="utf-8")

        invalid_offset = await self.tool.execute(path="notes.txt", offset=-1)
        invalid_limit = await self.tool.execute(path="notes.txt", limit=0)
        large_limit = await self.tool.execute(
            path="notes.txt",
            limit=self.tool.MAX_LIMIT + 1,
        )

        self.assertEqual(invalid_offset.error, "Offset must be a non-negative integer")
        self.assertEqual(
            invalid_limit.error,
            f"Limit must be an integer between 1 and {self.tool.MAX_LIMIT}",
        )
        self.assertEqual(
            large_limit.error,
            f"Limit must be an integer between 1 and {self.tool.MAX_LIMIT}",
        )

    def test_constructor_requires_an_existing_workspace_directory(self) -> None:
        with self.assertRaises(ValueError):
            ReadFileTool(self.root / "missing-workspace")
