"""Tests for workspace-contained filename and glob search."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nanobot.tools import Tool, ToolContext, ToolLoader, ToolRegistry
from nanobot.tools.builtin import FindFilesTool


class FindFilesToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.tool = FindFilesTool(self.workspace)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_finds_files_in_the_selected_directory(self) -> None:
        source = self.workspace / "src"
        source.mkdir()
        (source / "main.py").write_text("main", encoding="utf-8")
        (source / "helper.py").write_text("helper", encoding="utf-8")
        (self.workspace / "outside.py").write_text("outside", encoding="utf-8")

        result = await self.tool.execute(pattern="*.py", path="src")

        self.assertIsInstance(self.tool, Tool)
        self.assertTrue(result.success)
        self.assertEqual(result.content, "src/helper.py\nsrc/main.py")

    async def test_supports_filename_globs_without_reading_file_contents(self) -> None:
        (self.workspace / "README.md").write_text("readme", encoding="utf-8")
        (self.workspace / "notes.txt").write_text("notes", encoding="utf-8")

        with patch.object(Path, "read_text", side_effect=AssertionError):
            result = await self.tool.execute(pattern="*.md")

        self.assertTrue(result.success)
        self.assertEqual(result.content, "README.md")

    async def test_supports_recursive_double_star_globs(self) -> None:
        package = self.workspace / "package" / "nested"
        package.mkdir(parents=True)
        (self.workspace / "root.py").write_text("root", encoding="utf-8")
        (self.workspace / "package" / "module.py").write_text(
            "module",
            encoding="utf-8",
        )
        (package / "leaf.py").write_text("leaf", encoding="utf-8")
        (package / "readme.md").write_text("readme", encoding="utf-8")

        result = await self.tool.execute(pattern="**/*.py")

        self.assertTrue(result.success)
        self.assertEqual(
            result.content,
            "package/module.py\npackage/nested/leaf.py\nroot.py",
        )

    async def test_returns_workspace_relative_paths_in_stable_order(self) -> None:
        for name in ("zeta.py", "Alpha.py", "beta.py"):
            (self.workspace / name).write_text(name, encoding="utf-8")

        result = await self.tool.execute(pattern="*.py")

        self.assertTrue(result.success)
        self.assertEqual(result.content, "Alpha.py\nbeta.py\nzeta.py")
        self.assertNotIn(str(self.workspace), result.content)

    async def test_limits_the_number_of_returned_files(self) -> None:
        for name in ("alpha.py", "beta.py", "gamma.py"):
            (self.workspace / name).write_text(name, encoding="utf-8")

        result = await self.tool.execute(pattern="*.py", limit=2)

        self.assertTrue(result.success)
        self.assertEqual(result.content, "alpha.py\nbeta.py")

    async def test_rejects_absolute_and_parent_search_paths(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()

        absolute = await self.tool.execute(pattern="*.py", path=str(outside))
        parent = await self.tool.execute(pattern="*.py", path="../outside")
        parent_pattern = await self.tool.execute(pattern="../*.py")

        self.assertFalse(absolute.success)
        self.assertEqual(absolute.error, "Search path must be relative to the workspace")
        self.assertFalse(parent.success)
        self.assertEqual(parent.error, "Search path must not contain '..'")
        self.assertFalse(parent_pattern.success)
        self.assertEqual(parent_pattern.error, "Pattern must not contain '..'")

    async def test_does_not_follow_symbolic_links_outside_the_workspace(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private.py").write_text("private", encoding="utf-8")
        link = self.workspace / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"Symbolic links are unavailable: {error}")

        result = await self.tool.execute(pattern="**/*.py")
        direct_link_search = await self.tool.execute(pattern="*.py", path="linked")

        self.assertTrue(result.success)
        self.assertEqual(result.content, "(no files found)")
        self.assertFalse(direct_link_search.success)
        self.assertEqual(
            direct_link_search.error,
            "Path is outside the workspace: linked",
        )

    async def test_skips_generated_directories_by_default(self) -> None:
        for directory_name in (".git", "node_modules", "dist", ".vite"):
            directory = self.workspace / directory_name
            directory.mkdir()
            (directory / "generated.py").write_text("generated", encoding="utf-8")
        source = self.workspace / "source"
        source.mkdir()
        (source / "main.py").write_text("main", encoding="utf-8")

        result = await self.tool.execute(pattern="**/*.py")

        self.assertTrue(result.success)
        self.assertEqual(result.content, "source/main.py")

    async def test_returns_an_error_for_a_missing_search_directory(self) -> None:
        result = await self.tool.execute(pattern="*.py", path="missing")

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Directory not found: missing")

    async def test_returns_an_error_when_directory_traversal_fails(self) -> None:
        with patch.object(Path, "iterdir", side_effect=OSError("access denied")):
            result = await self.tool.execute(pattern="*.py")

        self.assertFalse(result.success)
        self.assertEqual(
            result.error,
            "Unable to search files: . (access denied)",
        )

    async def test_loader_and_registry_discover_the_tool(self) -> None:
        (self.workspace / "main.py").write_text("main", encoding="utf-8")
        registry = ToolRegistry()
        names = ToolLoader().load(registry, ToolContext(workspace=self.workspace))

        self.assertIn("find_files", names)
        self.assertIsInstance(registry.get("find_files"), FindFilesTool)
        result = await registry.execute("find_files", {"pattern": "*.py"})

        self.assertTrue(result.success)
        self.assertEqual(result.content, "main.py")
