"""Tests for the centralized tool registry."""

from __future__ import annotations

import unittest
from typing import Any

from nanobot.tools import Tool, ToolRegistry, ToolResult
from tests.tools.fakes import WeatherTool


class FailingTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="failing",
            description="Always fails during execution.",
            parameters=(),
        )

    async def execute(self, **arguments: Any) -> ToolResult:
        raise RuntimeError("service unavailable")


class ToolRegistryTest(unittest.IsolatedAsyncioTestCase):
    def test_register_find_remove_and_preserve_order(self) -> None:
        weather = WeatherTool()
        failing = FailingTool()
        registry = ToolRegistry()

        registry.register(weather)
        registry.register(failing)

        self.assertTrue(registry.has("get_weather"))
        self.assertIs(registry.get("get_weather"), weather)
        self.assertEqual(registry.tools, (weather, failing))
        self.assertIs(registry.remove("get_weather"), weather)
        self.assertFalse(registry.has("get_weather"))
        self.assertIsNone(registry.remove("get_weather"))

    def test_returns_provider_neutral_schemas_in_registration_order(self) -> None:
        weather = WeatherTool()
        registry = ToolRegistry((weather,))

        self.assertEqual(
            registry.schemas,
            (
                {
                    "name": "get_weather",
                    "description": "Get the current weather for a city.",
                    "parameters": weather.parameters_schema,
                },
            ),
        )

    async def test_executes_a_registered_tool_with_valid_arguments(self) -> None:
        weather = WeatherTool()
        registry = ToolRegistry((weather,))

        result = await registry.execute("get_weather", {"city": "Beijing"})

        self.assertEqual(result, ToolResult(content="Beijing: sunny"))
        self.assertEqual(weather.received_arguments, {"city": "Beijing"})

    async def test_returns_an_error_for_an_unknown_tool(self) -> None:
        result = await ToolRegistry().execute("missing", {})

        self.assertFalse(result.success)
        self.assertEqual(result.error, "Unknown tool: missing")

    async def test_rejects_invalid_arguments_without_executing_the_tool(self) -> None:
        weather = WeatherTool()
        registry = ToolRegistry((weather,))

        missing = await registry.execute("get_weather", {})
        invalid_type = await registry.execute("get_weather", {"city": 1})
        unexpected = await registry.execute(
            "get_weather",
            {"city": "Beijing", "days": 1},
        )

        self.assertEqual(
            missing.error,
            "Missing required parameter for get_weather: city",
        )
        self.assertEqual(
            invalid_type.error,
            "Invalid string parameter for get_weather: city",
        )
        self.assertEqual(
            unexpected.error,
            "Unknown parameter for get_weather: days",
        )
        self.assertIsNone(weather.received_arguments)

    async def test_converts_tool_execution_exceptions_to_errors(self) -> None:
        registry = ToolRegistry((FailingTool(),))

        result = await registry.execute("failing", {})

        self.assertFalse(result.success)
        self.assertEqual(
            result.error,
            "Tool execution failed: failing (service unavailable)",
        )

    def test_rejects_duplicate_tool_names(self) -> None:
        registry = ToolRegistry((WeatherTool(),))

        with self.assertRaisesRegex(
            ValueError,
            "Tool is already registered: get_weather",
        ):
            registry.register(WeatherTool())
