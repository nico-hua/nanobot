import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nanobot.tools import Tool
from nanobot.tools.builtin import EditFileTool


class EditFileToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.tool = EditFileTool(self.workspace)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_replaces_one_exact_text_occurrence(self) -> None:
        path = self.workspace / "notes.txt"
        path.write_text("Hello, world!", encoding="utf-8")

        result = await self.tool.execute(
            path="notes.txt",
            old_text="world",
            new_text="Agent",
        )

        self.assertIsInstance(self.tool, Tool)
        self.assertTrue(result.success)
        self.assertEqual(result.content, "Replaced text in notes.txt")
        self.assertEqual(path.read_text(encoding="utf-8"), "Hello, Agent!")

    async def test_returns_an_error_without_writing_when_text_is_not_found(self) -> None:
        path = self.workspace / "notes.txt"
        original_content = "Hello, world!"
        path.write_text(original_content, encoding="utf-8")

        result = await self.tool.execute(
            path="notes.txt",
            old_text="missing",
            new_text="Agent",
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Text not found in file: notes.txt")
        self.assertEqual(path.read_text(encoding="utf-8"), original_content)

    async def test_returns_an_error_without_writing_when_text_appears_multiple_times(
        self,
    ) -> None:
        path = self.workspace / "notes.txt"
        original_content = "repeat repeat"
        path.write_text(original_content, encoding="utf-8")

        result = await self.tool.execute(
            path="notes.txt",
            old_text="repeat",
            new_text="updated",
        )

        self.assertFalse(result.success)
        self.assertEqual(
            result.error,
            "Text must appear exactly once in file: notes.txt",
        )
        self.assertEqual(path.read_text(encoding="utf-8"), original_content)

    async def test_returns_an_error_for_a_missing_file(self) -> None:
        result = await self.tool.execute(
            path="missing.txt",
            old_text="old",
            new_text="new",
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error, "File not found: missing.txt")

    async def test_rejects_a_path_outside_the_workspace(self) -> None:
        outside_file = self.root / "private.txt"
        outside_file.write_text("private", encoding="utf-8")

        result = await self.tool.execute(
            path="../private.txt",
            old_text="private",
            new_text="changed",
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Path is outside the workspace: ../private.txt")
        self.assertEqual(outside_file.read_text(encoding="utf-8"), "private")

    async def test_returns_an_error_without_writing_for_non_utf8_text(self) -> None:
        path = self.workspace / "binary.dat"
        original_content = b"\xff\xfe"
        path.write_bytes(original_content)

        result = await self.tool.execute(
            path="binary.dat",
            old_text="old",
            new_text="new",
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error, "File is not valid UTF-8 text: binary.dat")
        self.assertEqual(path.read_bytes(), original_content)

    async def test_returns_an_error_without_writing_when_replacement_is_identical(
        self,
    ) -> None:
        path = self.workspace / "notes.txt"
        original_content = "unchanged"
        path.write_text(original_content, encoding="utf-8")

        result = await self.tool.execute(
            path="notes.txt",
            old_text="unchanged",
            new_text="unchanged",
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Old text and new text must differ")
        self.assertEqual(path.read_text(encoding="utf-8"), original_content)

    async def test_preserves_the_original_file_when_saving_fails(self) -> None:
        path = self.workspace / "notes.txt"
        original_content = "old content"
        path.write_text(original_content, encoding="utf-8")

        with patch.object(Path, "replace", side_effect=OSError("disk full")):
            result = await self.tool.execute(
                path="notes.txt",
                old_text="old",
                new_text="new",
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Unable to save file: notes.txt (disk full)")
        self.assertEqual(path.read_text(encoding="utf-8"), original_content)

    def test_constructor_requires_an_existing_workspace_directory(self) -> None:
        with self.assertRaises(ValueError):
            EditFileTool(self.root / "missing-workspace")
