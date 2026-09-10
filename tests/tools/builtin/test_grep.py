"""Tests for workspace-contained regular-expression content search."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nanobot.tools import Tool, ToolContext, ToolLoader, ToolRegistry
from nanobot.tools.builtin import FindFilesTool, GrepTool


class GrepToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.tool = GrepTool(self.workspace)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_searches_text_with_regular_expressions_and_line_numbers(self) -> None:
        source = self.workspace / "src"
        source.mkdir()
        (source / "main.py").write_text(
            "first line\nvalue-42\nlast line\n",
            encoding="utf-8",
        )

        result = await self.tool.execute(pattern=r"value-\d{2}")

        self.assertIsInstance(self.tool, Tool)
        self.assertTrue(result.success)
        self.assertEqual(result.content, "src/main.py:2: value-42")

    async def test_include_filters_files_by_name_across_directories(self) -> None:
        package = self.workspace / "package"
        package.mkdir()
        (self.workspace / "notes.txt").write_text("needle", encoding="utf-8")
        (self.workspace / "main.py").write_text("needle", encoding="utf-8")
        (package / "helper.py").write_text("needle", encoding="utf-8")

        result = await self.tool.execute(pattern="needle", include="*.py")

        self.assertTrue(result.success)
        self.assertEqual(
            result.content,
            "main.py:1: needle\npackage/helper.py:1: needle",
        )

    async def test_case_sensitivity_can_be_disabled(self) -> None:
        (self.workspace / "notes.txt").write_text(
            "Needle\nneedle\n",
            encoding="utf-8",
        )

        sensitive = await self.tool.execute(pattern="needle")
        insensitive = await self.tool.execute(
            pattern="needle",
            case_sensitive=False,
        )

        self.assertEqual(sensitive.content, "notes.txt:2: needle")
        self.assertEqual(
            insensitive.content,
            "notes.txt:1: Needle\nnotes.txt:2: needle",
        )

    async def test_limits_results_in_stable_path_and_line_order(self) -> None:
        source = self.workspace / "source"
        source.mkdir()
        (self.workspace / "zeta.txt").write_text("needle", encoding="utf-8")
        (source / "alpha.txt").write_text(
            "needle one\nneedle two\n",
            encoding="utf-8",
        )

        all_matches = await self.tool.execute(pattern="needle")
        limited_matches = await self.tool.execute(pattern="needle", limit=2)

        self.assertEqual(
            all_matches.content,
            "source/alpha.txt:1: needle one\n"
            "source/alpha.txt:2: needle two\n"
            "zeta.txt:1: needle",
        )
        self.assertEqual(
            limited_matches.content,
            "source/alpha.txt:1: needle one\nsource/alpha.txt:2: needle two",
        )

    async def test_bounds_long_lines_and_total_output(self) -> None:
        long_line = f"needle {'x' * 100}"
        (self.workspace / "long.txt").write_text(
            f"{long_line}\n{long_line}\n",
            encoding="utf-8",
        )

        with (
            patch.object(GrepTool, "MAX_LINE_CHARACTERS", 40),
            patch.object(GrepTool, "MAX_OUTPUT_CHARACTERS", 90),
        ):
            result = await self.tool.execute(pattern="needle")

        self.assertTrue(result.success)
        self.assertLessEqual(len(result.content), 90)
        self.assertIn("[line truncated]", result.content)
        self.assertIn("[output truncated]", result.content)

    async def test_rejects_absolute_and_parent_paths(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()

        absolute = await self.tool.execute(pattern="needle", path=str(outside))
        parent = await self.tool.execute(pattern="needle", path="../outside")

        self.assertFalse(absolute.success)
        self.assertEqual(absolute.error, "Search path must be relative to the workspace")
        self.assertFalse(parent.success)
        self.assertEqual(parent.error, "Search path must not contain '..'")

    async def test_does_not_follow_symbolic_links_outside_the_workspace(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private.txt").write_text("needle", encoding="utf-8")
        link = self.workspace / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"Symbolic links are unavailable: {error}")

        result = await self.tool.execute(pattern="needle")
        direct_link_search = await self.tool.execute(pattern="needle", path="linked")

        self.assertTrue(result.success)
        self.assertEqual(result.content, "(no matches)")
        self.assertFalse(direct_link_search.success)
        self.assertEqual(
            direct_link_search.error,
            "Path is outside the workspace: linked",
        )

    async def test_skips_generated_directories_by_default(self) -> None:
        for directory_name in (".git", "node_modules", "dist", ".vite"):
            directory = self.workspace / directory_name
            directory.mkdir()
            (directory / "generated.txt").write_text("needle", encoding="utf-8")
        source = self.workspace / "source"
        source.mkdir()
        (source / "main.txt").write_text("needle", encoding="utf-8")

        result = await self.tool.execute(pattern="needle")

        self.assertTrue(result.success)
        self.assertEqual(result.content, "source/main.txt:1: needle")

    async def test_returns_clear_errors_for_invalid_pattern_and_missing_directory(self) -> None:
        invalid_pattern = await self.tool.execute(pattern="[")
        missing_directory = await self.tool.execute(pattern="needle", path="missing")

        self.assertFalse(invalid_pattern.success)
        self.assertTrue(
            invalid_pattern.error.startswith("Invalid regular expression:")
        )
        self.assertFalse(missing_directory.success)
        self.assertEqual(missing_directory.error, "Directory not found: missing")

    async def test_skips_non_utf8_files_and_reports_read_failures(self) -> None:
        (self.workspace / "valid.txt").write_text("needle", encoding="utf-8")
        (self.workspace / "binary.dat").write_bytes(b"\xff\xfe\x00needle")

        skipped_binary = await self.tool.execute(pattern="needle")
        with patch.object(Path, "open", side_effect=OSError("access denied")):
            read_failure = await self.tool.execute(pattern="needle")

        self.assertTrue(skipped_binary.success)
        self.assertEqual(skipped_binary.content, "valid.txt:1: needle")
        self.assertFalse(read_failure.success)
        self.assertEqual(
            read_failure.error,
            "Unable to search files: . (access denied)",
        )

    async def test_loader_and_registry_discover_grep_without_affecting_find_files(self) -> None:
        (self.workspace / "main.py").write_text("needle", encoding="utf-8")
        registry = ToolRegistry()
        names = ToolLoader().load(registry, ToolContext(workspace=self.workspace))

        self.assertIn("grep", names)
        self.assertIsInstance(registry.get("grep"), GrepTool)
        self.assertIsInstance(registry.get("find_files"), FindFilesTool)
        result = await registry.execute("grep", {"pattern": "needle"})

        self.assertTrue(result.success)
        self.assertEqual(result.content, "main.py:1: needle")
