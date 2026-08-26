import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nanobot.tools import Tool
from nanobot.tools.builtin import WriteFileTool


class WriteFileToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.tool = WriteFileTool(self.workspace)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_creates_a_workspace_relative_utf8_file(self) -> None:
        result = await self.tool.execute(path="notes.txt", content="Hello, 世界")

        self.assertIsInstance(self.tool, Tool)
        self.assertTrue(result.success)
        self.assertEqual(result.content, "Wrote 9 characters to notes.txt")
        self.assertEqual(
            (self.workspace / "notes.txt").read_text(encoding="utf-8"),
            "Hello, 世界",
        )

    async def test_overwrites_an_existing_file(self) -> None:
        path = self.workspace / "notes.txt"
        path.write_text("old content", encoding="utf-8")

        result = await self.tool.execute(path="notes.txt", content="new content")

        self.assertTrue(result.success)
        self.assertEqual(path.read_text(encoding="utf-8"), "new content")

    async def test_creates_missing_parent_directories(self) -> None:
        result = await self.tool.execute(
            path="nested/notes/today.txt",
            content="planned work",
        )

        self.assertTrue(result.success)
        self.assertEqual(
            (self.workspace / "nested" / "notes" / "today.txt").read_text(
                encoding="utf-8"
            ),
            "planned work",
        )

    async def test_allows_empty_content(self) -> None:
        path = self.workspace / "empty.txt"
        path.write_text("old content", encoding="utf-8")

        result = await self.tool.execute(path="empty.txt", content="")

        self.assertTrue(result.success)
        self.assertEqual(result.content, "Wrote 0 characters to empty.txt")
        self.assertEqual(path.read_text(encoding="utf-8"), "")

    async def test_rejects_a_path_outside_the_workspace(self) -> None:
        result = await self.tool.execute(path="../private.txt", content="private")

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Path is outside the workspace: ../private.txt")
        self.assertFalse((self.root / "private.txt").exists())

    async def test_returns_errors_for_invalid_argument_types(self) -> None:
        empty_path = await self.tool.execute(path="", content="content")
        invalid_path = await self.tool.execute(
            path=42,  # type: ignore[arg-type]
            content="content",
        )
        invalid_content = await self.tool.execute(
            path="notes.txt",
            content=42,  # type: ignore[arg-type]
        )

        self.assertEqual(empty_path.error, "File path must be a non-empty string")
        self.assertEqual(invalid_path.error, "File path must be a non-empty string")
        self.assertEqual(invalid_content.error, "File content must be a string")

    async def test_returns_an_error_when_writing_fails(self) -> None:
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            result = await self.tool.execute(path="notes.txt", content="content")

        self.assertFalse(result.success)
        self.assertEqual(
            result.error,
            "Unable to write file: notes.txt (disk full)",
        )

    def test_constructor_requires_an_existing_workspace_directory(self) -> None:
        with self.assertRaises(ValueError):
            WriteFileTool(self.root / "missing-workspace")
