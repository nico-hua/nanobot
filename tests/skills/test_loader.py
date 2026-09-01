"""Focused tests for static Markdown Skill discovery and reading."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nanobot.skills import SkillNotFoundError, SkillsLoader


class SkillsLoaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._workspace = Path(self._temporary_directory.name) / "workspace"
        self._workspace.mkdir()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_discovers_workspace_skills_with_metadata(self) -> None:
        path = self._write_skill(
            self._workspace,
            "review",
            "---\nname: code-review\ndescription: Review Python changes.\n---\nUse focused checks.\n",
        )

        skills = self._loader().list_skills()

        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].name, "code-review")
        self.assertEqual(skills[0].description, "Review Python changes.")
        self.assertEqual(skills[0].path, path)
        self.assertFalse(skills[0].always)

    def test_parses_top_level_and_nested_always_metadata(self) -> None:
        self._write_skill(
            self._workspace,
            "top-level",
            "---\nname: top-level\nalways: true\n---\nTop-level body.\n",
        )
        self._write_skill(
            self._workspace,
            "nested",
            "---\nname: nested\nmetadata:\n  nanobot:\n    always: true\n---\nNested body.\n",
        )

        skills = self._loader().list_skills()

        self.assertTrue(all(skill.always for skill in skills))

    @patch("nanobot.skills.loader.shutil.which", return_value="C:/tools/gh")
    def test_marks_skill_available_when_required_bin_exists(self, which: object) -> None:
        self._write_skill(
            self._workspace,
            "github",
            "---\nname: github\nmetadata:\n  nanobot:\n    requires:\n      bins: [\"gh\"]\n---\nGitHub body.\n",
        )

        skill = self._loader().list_skills()[0]

        self.assertTrue(skill.is_available)
        self.assertEqual(skill.missing_dependencies, ())

    @patch("nanobot.skills.loader.shutil.which", return_value=None)
    def test_marks_skill_unavailable_when_required_bin_is_missing(self, which: object) -> None:
        self._write_skill(
            self._workspace,
            "github",
            "---\nname: github\nnanobot:\n  requires:\n    bins:\n      - gh\n---\nGitHub body.\n",
        )

        skill = self._loader().list_skills()[0]

        self.assertFalse(skill.is_available)
        self.assertEqual(skill.missing_dependencies, ("bin: gh",))

    @patch("nanobot.skills.loader.shutil.which", return_value=None)
    def test_reads_requirements_from_inline_json_metadata(
        self,
        which: object,
    ) -> None:
        self._write_skill(
            self._workspace,
            "github",
            "---\n"
            "name: github\n"
            'metadata: {"nanobot":{"emoji":"🐙","requires":{"bins":["gh"]},"install":[{"id":"brew","kind":"brew","formula":"gh","bins":["gh"],"label":"Install GitHub CLI (brew)"}]}}\n'
            "---\nGitHub body.\n",
        )

        skill = self._loader().list_skills()[0]

        self.assertFalse(skill.is_available)
        self.assertEqual(skill.missing_dependencies, ("bin: gh",))

    @patch("nanobot.skills.loader.shutil.which", return_value=None)
    def test_reads_requirements_from_json_frontmatter(self, which: object) -> None:
        self._write_skill(
            self._workspace,
            "github",
            "---\n"
            '{"nanobot":{"requires":{"bins":["gh"]}}}\n'
            "---\nGitHub body.\n",
        )

        skill = self._loader().list_skills()[0]

        self.assertFalse(skill.is_available)
        self.assertEqual(skill.missing_dependencies, ("bin: gh",))

    def test_marks_skill_available_when_required_environment_variable_is_set(self) -> None:
        self._write_skill(
            self._workspace,
            "github",
            "---\nname: github\nnanobot:\n  requires:\n    env: [\"GITHUB_TOKEN\"]\n---\nGitHub body.\n",
        )

        with patch.dict(os.environ, {"GITHUB_TOKEN": "present"}, clear=True):
            skill = self._loader().list_skills()[0]

        self.assertTrue(skill.is_available)
        self.assertEqual(skill.missing_dependencies, ())

    def test_marks_skill_unavailable_when_required_environment_variable_is_blank(self) -> None:
        self._write_skill(
            self._workspace,
            "github",
            "---\nname: github\nnanobot.requires.env: [\"GITHUB_TOKEN\"]\n---\nGitHub body.\n",
        )

        with patch.dict(os.environ, {"GITHUB_TOKEN": ""}, clear=True):
            skill = self._loader().list_skills()[0]

        self.assertFalse(skill.is_available)
        self.assertEqual(skill.missing_dependencies, ("env: GITHUB_TOKEN",))

    @patch("nanobot.skills.loader.shutil.which", return_value=None)
    def test_reports_all_missing_dependencies_in_a_stable_order(self, which: object) -> None:
        self._write_skill(
            self._workspace,
            "github",
            "---\nname: github\nnanobot:\n  requires:\n    bins: [\"gh\", \"jq\"]\n    env: [\"GITHUB_TOKEN\", \"SECOND_TOKEN\"]\n---\nGitHub body.\n",
        )

        with patch.dict(os.environ, {}, clear=True):
            skill = self._loader().list_skills()[0]

        self.assertFalse(skill.is_available)
        self.assertEqual(
            skill.missing_dependencies,
            ("bin: gh", "bin: jq", "env: GITHUB_TOKEN", "env: SECOND_TOKEN"),
        )

    def test_ignores_invalid_requirement_entries_without_breaking_discovery(self) -> None:
        self._write_skill(
            self._workspace,
            "invalid",
            "---\nname: invalid\nnanobot:\n  requires:\n    bins: [\"gh\"\n    env: not/an/environment-variable\n---\nStatic body.\n",
        )

        skill = self._loader().list_skills()[0]

        self.assertTrue(skill.is_available)
        self.assertEqual(skill.missing_dependencies, ())

    @patch("nanobot.skills.loader.shutil.which", return_value=None)
    def test_ignores_legacy_requirement_paths(self, which: object) -> None:
        self._write_skill(
            self._workspace,
            "github",
            "---\nname: github\nrequires:\n  bins: [\"gh\"]\n---\nGitHub body.\n",
        )

        skill = self._loader().list_skills()[0]

        self.assertTrue(skill.is_available)
        self.assertEqual(skill.missing_dependencies, ())

    def test_rechecks_current_path_and_environment_for_each_listing(self) -> None:
        self._write_skill(
            self._workspace,
            "github",
            "---\nname: github\nnanobot:\n  requires:\n    bins: [\"gh\"]\n    env: [\"GITHUB_TOKEN\"]\n---\nGitHub body.\n",
        )
        loader = self._loader()

        with patch("nanobot.skills.loader.shutil.which", return_value=None), patch.dict(
            os.environ,
            {},
            clear=True,
        ):
            unavailable = loader.list_skills()[0]
        with patch(
            "nanobot.skills.loader.shutil.which",
            return_value="C:/tools/gh",
        ), patch.dict(os.environ, {"GITHUB_TOKEN": "present"}, clear=True):
            available = loader.list_skills()[0]

        self.assertFalse(unavailable.is_available)
        self.assertTrue(available.is_available)

    def test_reads_the_markdown_body_without_frontmatter(self) -> None:
        self._write_skill(
            self._workspace,
            "review",
            "---\nname: review\ndescription: Review changes.\n---\n# Review\n\nCheck tests.\n",
        )

        content = self._loader().read_skill("review")

        self.assertEqual(content, "# Review\n\nCheck tests.\n")
        self.assertNotIn("description:", content)
        self.assertNotIn("---", content)

    def test_uses_the_directory_name_when_frontmatter_is_absent(self) -> None:
        self._write_skill(
            self._workspace,
            "plain",
            "# Plain Skill\n\nNo frontmatter is required.\n",
        )

        skills = self._loader().list_skills()

        self.assertEqual(skills[0].name, "plain")
        self.assertEqual(skills[0].description, "")
        self.assertEqual(
            self._loader().read_skill("plain"),
            "# Plain Skill\n\nNo frontmatter is required.\n",
        )

    def test_lists_multiple_workspace_skills_in_a_stable_name_order(self) -> None:
        self._write_skill(
            self._workspace,
            "second",
            "---\nname: zeta\ndescription: Second Skill.\n---\nSecond body.\n",
        )
        self._write_skill(
            self._workspace,
            "first",
            "---\nname: alpha\ndescription: First Skill.\n---\nFirst body.\n",
        )

        skills = self._loader().list_skills()

        self.assertEqual(tuple(skill.name for skill in skills), ("alpha", "zeta"))
        self.assertEqual(self._loader().read_skill("alpha"), "First body.\n")
        self.assertEqual(self._loader().read_skill("zeta"), "Second body.\n")

    def test_finds_only_known_skill_references_in_message_order_without_duplicates(self) -> None:
        self._write_skill(
            self._workspace,
            "github",
            "---\nname: github\n---\nGitHub body.\n",
        )
        self._write_skill(
            self._workspace,
            "weather",
            "---\nname: weather\n---\nWeather body.\n",
        )

        skills = self._loader().find_referenced_skills(
            "Use $weather, then $github, then $weather again; ignore $unknown, $github/../outside, and $github.file."
        )

        self.assertEqual(tuple(skill.name for skill in skills), ("weather", "github"))

    def test_missing_directories_or_skill_files_do_not_break_discovery(self) -> None:
        (self._workspace / "skills" / "missing-file").mkdir(parents=True)

        self.assertEqual(self._loader().list_skills(), ())
        with self.assertRaisesRegex(SkillNotFoundError, "Skill was not found: missing-file"):
            self._loader().read_skill("missing-file")

    def test_invalid_frontmatter_falls_back_without_affecting_other_skills(self) -> None:
        self._write_skill(
            self._workspace,
            "invalid",
            "---\nname invalid\n---\nInvalid metadata body.\n",
        )
        self._write_skill(
            self._workspace,
            "valid",
            "---\nname: valid\ndescription: Valid metadata.\n---\nValid body.\n",
        )

        skills = self._loader().list_skills()

        self.assertEqual(tuple(skill.name for skill in skills), ("invalid", "valid"))
        self.assertEqual(skills[0].description, "")
        self.assertEqual(self._loader().read_skill("invalid"), "Invalid metadata body.\n")
        self.assertEqual(self._loader().read_skill("valid"), "Valid body.\n")

    def test_reading_a_skill_does_not_execute_its_contents(self) -> None:
        self._write_skill(
            self._workspace,
            "static",
            "---\nname: static\ndescription: Static instructions.\n---\npython -c \\\"raise RuntimeError('must not run')\\\"\n",
        )

        content = self._loader().read_skill("static")

        self.assertIn("raise RuntimeError('must not run')", content)

    def _loader(self) -> SkillsLoader:
        return SkillsLoader(self._workspace)

    @staticmethod
    def _write_skill(root: Path, directory_name: str, content: str) -> Path:
        path = root / "skills" / directory_name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path
