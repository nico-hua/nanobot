import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nanobot.tools import Tool
from nanobot.tools.builtin import ListDirTool


class ListDirToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.tool = ListDirTool(self.workspace)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_lists_workspace_entries_in_stable_order(self) -> None:
        (self.workspace / "notes.txt").write_text("notes", encoding="utf-8")
        (self.workspace / "docs").mkdir()
        (self.workspace / "alpha.txt").write_text("alpha", encoding="utf-8")

        result = await self.tool.execute()

        self.assertIsInstance(self.tool, Tool)
        self.assertTrue(result.success)
        self.assertEqual(
            result.content,
            "[file] alpha.txt\n[dir] docs\n[file] notes.txt",
        )

    async def test_recursively_lists_nested_entries(self) -> None:
        docs = self.workspace / "docs"
        reference = docs / "reference"
        reference.mkdir(parents=True)
        (docs / "guide.txt").write_text("guide", encoding="utf-8")
        (reference / "api.txt").write_text("api", encoding="utf-8")
        (self.workspace / "README.md").write_text("readme", encoding="utf-8")

        result = await self.tool.execute(recursive=True)

        self.assertTrue(result.success)
        self.assertEqual(
            result.content,
            "[dir] docs\n"
            "[file] docs/guide.txt\n"
            "[dir] docs/reference\n"
            "[file] docs/reference/api.txt\n"
            "[file] README.md",
        )

    async def test_limits_the_number_of_returned_entries(self) -> None:
        for name in ("alpha.txt", "beta.txt", "gamma.txt"):
            (self.workspace / name).write_text(name, encoding="utf-8")

        result = await self.tool.execute(limit=2)

        self.assertTrue(result.success)
        self.assertEqual(result.content, "[file] alpha.txt\n[file] beta.txt")

    async def test_rejects_a_path_outside_the_workspace(self) -> None:
        outside_directory = self.root / "private"
        outside_directory.mkdir()

        result = await self.tool.execute(path="../private")

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Path is outside the workspace: ../private")

    async def test_returns_an_error_for_a_missing_directory(self) -> None:
        result = await self.tool.execute(path="missing")

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Directory not found: missing")

    async def test_returns_an_error_for_a_file_path(self) -> None:
        (self.workspace / "notes.txt").write_text("notes", encoding="utf-8")

        result = await self.tool.execute(path="notes.txt")

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Path is not a directory: notes.txt")

    async def test_returns_an_error_when_listing_fails(self) -> None:
        with patch.object(Path, "iterdir", side_effect=OSError("access denied")):
            result = await self.tool.execute()

        self.assertFalse(result.success)
        self.assertEqual(
            result.error,
            "Unable to list directory: . (access denied)",
        )
