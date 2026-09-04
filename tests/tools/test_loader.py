"""Tests for automatic discovery of built-in tools."""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from nanobot.cron import CronService
from nanobot.tools import Tool, ToolContext, ToolLoader, ToolRegistry
from nanobot.tools.builtin import CronTool


class ToolLoaderTest(unittest.TestCase):
    EXPECTED_TOOL_NAMES = (
        "edit_file",
        "exec",
        "list_dir",
        "read_file",
        "write_file",
    )

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self._temporary_directory.name)
        self.context = ToolContext(workspace=self.workspace)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_discovers_and_registers_concrete_builtin_tools(self) -> None:
        registry = ToolRegistry()

        names = ToolLoader().load(registry, self.context)

        self.assertEqual(names, self.EXPECTED_TOOL_NAMES)
        self.assertEqual(tuple(tool.name for tool in registry.tools), names)
        self.assertTrue(all(tool.workspace == self.workspace for tool in registry.tools))

    def test_skips_base_and_abstract_tools(self) -> None:
        registry = ToolRegistry()

        ToolLoader().load(registry, self.context)

        self.assertFalse(registry.has("Tool"))
        self.assertTrue(
            all(
                isinstance(tool, Tool) and not inspect.isabstract(type(tool))
                for tool in registry.tools
            )
        )

    def test_registered_tools_expose_schemas(self) -> None:
        registry = ToolRegistry()

        ToolLoader().load(registry, self.context)

        self.assertEqual(
            tuple(schema["name"] for schema in registry.schemas),
            self.EXPECTED_TOOL_NAMES,
        )

    def test_discovery_order_is_stable(self) -> None:
        first_registry = ToolRegistry()
        second_registry = ToolRegistry()
        loader = ToolLoader()

        first_names = loader.load(first_registry, self.context)
        second_names = loader.load(second_registry, self.context)

        self.assertEqual(first_names, self.EXPECTED_TOOL_NAMES)
        self.assertEqual(second_names, first_names)

    def test_loading_twice_does_not_register_duplicate_tools(self) -> None:
        registry = ToolRegistry()
        loader = ToolLoader()

        first_names = loader.load(registry, self.context)
        second_names = loader.load(registry, self.context)

        self.assertEqual(first_names, self.EXPECTED_TOOL_NAMES)
        self.assertEqual(second_names, ())
        self.assertEqual(tuple(tool.name for tool in registry.tools), first_names)

    def test_skips_workspace_tools_when_workspace_is_unavailable(self) -> None:
        registry = ToolRegistry()

        names = ToolLoader().load(registry, ToolContext())

        self.assertEqual(names, ())
        self.assertEqual(registry.tools, ())

    def test_registers_cron_tool_when_the_service_is_injected(self) -> None:
        registry = ToolRegistry()
        service = CronService(_no_op, self.workspace)

        names = ToolLoader().load(
            registry,
            ToolContext(
                workspace=self.workspace,
                cron_service=service,
                cron_timezone="Asia/Shanghai",
            ),
        )

        self.assertEqual(names, ("cron", *self.EXPECTED_TOOL_NAMES))
        self.assertIsInstance(registry.get("cron"), CronTool)

async def _no_op(task: object) -> None:
    del task
