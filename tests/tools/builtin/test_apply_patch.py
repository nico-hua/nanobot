"""Tests for strict, atomic workspace patch application."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nanobot.tools import Tool, ToolContext, ToolLoader, ToolRegistry
from nanobot.tools.builtin import ApplyPatchTool


class ApplyPatchToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.tool = ApplyPatchTool(self.workspace)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_adds_a_utf8_file(self) -> None:
        result = await self.tool.execute(
            """*** Begin Patch
*** Add File: nested/notes.txt
+first line
+第二行
*** End Patch"""
        )

        self.assertIsInstance(self.tool, Tool)
        self.assertTrue(result.success)
        self.assertEqual(
            result.content,
            "Applied patch:\n- added nested/notes.txt (+2)",
        )
        self.assertEqual(
            (self.workspace / "nested" / "notes.txt").read_text(encoding="utf-8"),
            "first line\n第二行\n",
        )

    async def test_updates_an_existing_file_without_overwriting_other_content(self) -> None:
        path = self.workspace / "settings.py"
        path.write_text("old value\nkeep this\n", encoding="utf-8")

        result = await self.tool.execute(
            """*** Begin Patch
*** Update File: settings.py
@@
-old value
+new value
 keep this
*** End Patch"""
        )

        self.assertTrue(result.success)
        self.assertEqual(result.content, "Applied patch:\n- updated settings.py (+1/-1)")
        self.assertEqual(path.read_text(encoding="utf-8"), "new value\nkeep this\n")

    async def test_deletes_an_existing_text_file(self) -> None:
        path = self.workspace / "obsolete.txt"
        path.write_text("one\ntwo\n", encoding="utf-8")

        result = await self.tool.execute(
            """*** Begin Patch
*** Delete File: obsolete.txt
*** End Patch"""
        )

        self.assertTrue(result.success)
        self.assertEqual(result.content, "Applied patch:\n- deleted obsolete.txt (-2)")
        self.assertFalse(path.exists())

    async def test_applies_multiple_hunks_to_one_file(self) -> None:
        path = self.workspace / "notes.txt"
        path.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

        result = await self.tool.execute(
            """*** Begin Patch
*** Update File: notes.txt
@@
-one
+ONE
@@
-three
+THREE
*** End Patch"""
        )

        self.assertTrue(result.success)
        self.assertEqual(path.read_text(encoding="utf-8"), "ONE\ntwo\nTHREE\nfour\n")

    async def test_preserves_utf8_text(self) -> None:
        path = self.workspace / "greeting.txt"
        path.write_text("你好\n世界\n", encoding="utf-8")

        result = await self.tool.execute(
            """*** Begin Patch
*** Update File: greeting.txt
@@
 你好
-世界
+朋友
*** End Patch"""
        )

        self.assertTrue(result.success)
        self.assertEqual(path.read_text(encoding="utf-8"), "你好\n朋友\n")

    async def test_rejects_malformed_patches_without_writing(self) -> None:
        path = self.workspace / "notes.txt"
        path.write_text("original\n", encoding="utf-8")

        result = await self.tool.execute("*** Update File: notes.txt\n@@\n-original")

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Patch must start with '*** Begin Patch'")
        self.assertEqual(path.read_text(encoding="utf-8"), "original\n")

    async def test_rejects_missing_or_ambiguous_context_without_writing(self) -> None:
        path = self.workspace / "notes.txt"
        path.write_text("same\nsame\n", encoding="utf-8")

        missing = await self.tool.execute(
            """*** Begin Patch
*** Update File: notes.txt
@@
-missing
+updated
*** End Patch"""
        )
        ambiguous = await self.tool.execute(
            """*** Begin Patch
*** Update File: notes.txt
@@
-same
+updated
*** End Patch"""
        )

        self.assertEqual(missing.error, "Patch context not found in file: notes.txt")
        self.assertEqual(ambiguous.error, "Patch context is ambiguous in file: notes.txt")
        self.assertEqual(path.read_text(encoding="utf-8"), "same\nsame\n")

    async def test_rejects_a_missing_or_non_utf8_target_file(self) -> None:
        binary_path = self.workspace / "binary.dat"
        binary_path.write_bytes(b"\xff\xfe")

        missing = await self.tool.execute(
            """*** Begin Patch
*** Update File: missing.txt
@@
-old
+new
*** End Patch"""
        )
        binary = await self.tool.execute(
            """*** Begin Patch
*** Update File: binary.dat
@@
-old
+new
*** End Patch"""
        )

        self.assertEqual(missing.error, "File not found: missing.txt")
        self.assertEqual(binary.error, "File is not valid UTF-8 text: binary.dat")
        self.assertEqual(binary_path.read_bytes(), b"\xff\xfe")

    async def test_rejects_absolute_and_parent_paths(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")

        absolute = await self.tool.execute(
            f"""*** Begin Patch
*** Update File: {outside}
@@
-outside
+changed
*** End Patch"""
        )
        parent = await self.tool.execute(
            """*** Begin Patch
*** Update File: ../outside.txt
@@
-outside
+changed
*** End Patch"""
        )

        self.assertEqual(
            absolute.error,
            "Patch file path must be relative to the workspace",
        )
        self.assertEqual(parent.error, "Patch file path must not contain '..'")
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")

    async def test_rejects_symbolic_links_that_target_outside_the_workspace(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("private\n", encoding="utf-8")
        link = self.workspace / "linked.txt"
        try:
            link.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"Symbolic links are unavailable: {error}")

        result = await self.tool.execute(
            """*** Begin Patch
*** Update File: linked.txt
@@
-private
+changed
*** End Patch"""
        )

        self.assertFalse(result.success)
        self.assertEqual(
            result.error,
            "Patch file path must not use symbolic links: linked.txt",
        )
        self.assertEqual(outside.read_text(encoding="utf-8"), "private\n")

    async def test_save_failure_restores_the_original_file(self) -> None:
        path = self.workspace / "notes.txt"
        path.write_text("old\n", encoding="utf-8")

        with patch.object(Path, "replace", side_effect=OSError("disk full")):
            result = await self.tool.execute(
                """*** Begin Patch
*** Update File: notes.txt
@@
-old
+new
*** End Patch"""
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Unable to apply patch: disk full")
        self.assertEqual(path.read_text(encoding="utf-8"), "old\n")

    async def test_multi_file_save_failure_rolls_back_previously_written_files(self) -> None:
        first = self.workspace / "first.txt"
        second = self.workspace / "second.txt"
        first.write_text("first old\n", encoding="utf-8")
        second.write_text("second old\n", encoding="utf-8")
        original_replace = Path.replace
        replace_calls = 0

        def fail_on_fourth_replace(source: Path, target: Path) -> Path:
            nonlocal replace_calls
            replace_calls += 1
            if replace_calls == 4:
                raise OSError("disk full")
            return original_replace(source, target)

        with patch.object(Path, "replace", new=fail_on_fourth_replace):
            result = await self.tool.execute(
                """*** Begin Patch
*** Update File: first.txt
@@
-first old
+first new
*** Update File: second.txt
@@
-second old
+second new
*** End Patch"""
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Unable to apply patch: disk full")
        self.assertEqual(first.read_text(encoding="utf-8"), "first old\n")
        self.assertEqual(second.read_text(encoding="utf-8"), "second old\n")

    async def test_loader_and_registry_discover_the_tool(self) -> None:
        registry = ToolRegistry()
        names = ToolLoader().load(registry, ToolContext(workspace=self.workspace))

        self.assertIn("apply_patch", names)
        self.assertIsInstance(registry.get("apply_patch"), ApplyPatchTool)
        result = await registry.execute(
            "apply_patch",
            {
                "patch": """*** Begin Patch
*** Add File: created.txt
+created
*** End Patch""",
            },
        )

        self.assertTrue(result.success)
        self.assertEqual(
            (self.workspace / "created.txt").read_text(encoding="utf-8"),
            "created\n",
        )
