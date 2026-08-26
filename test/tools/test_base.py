import unittest
from typing import Any

from nanobot.tools import Tool, ToolContext, ToolParameter, ToolResult
from test.tools.fakes import WeatherTool


class EchoTool(Tool):
    async def execute(self, **arguments: Any) -> ToolResult:
        return ToolResult(content=str(arguments))


class ToolResultTest(unittest.TestCase):
    def test_result_keeps_content_success_and_error(self) -> None:
        success = ToolResult(content="Beijing: sunny")
        failure = ToolResult(content="", success=False, error="City was not found")

        self.assertTrue(success.success)
        self.assertIsNone(success.error)
        self.assertFalse(failure.success)
        self.assertEqual(failure.error, "City was not found")


class ToolParameterTest(unittest.TestCase):
    def test_parameter_keeps_its_metadata(self) -> None:
        parameter = ToolParameter(
            name="days",
            description="The number of forecast days.",
            type="integer",
            required=True,
        )

        self.assertEqual(parameter.name, "days")
        self.assertEqual(parameter.description, "The number of forecast days.")
        self.assertEqual(parameter.type, "integer")
        self.assertTrue(parameter.required)

    def test_parameter_rejects_unsupported_types(self) -> None:
        with self.assertRaises(ValueError):
            ToolParameter(
                name="cities",
                description="Cities to query.",
                type="array",  # type: ignore[arg-type]
            )


class ToolTest(unittest.TestCase):
    def test_tool_is_abstract(self) -> None:
        with self.assertRaises(TypeError):
            Tool(
                name="get_weather",
                description="Get weather.",
                parameters=(),
            )

    def test_constructor_requires_metadata_and_tool_parameters(self) -> None:
        with self.assertRaises(ValueError):
            EchoTool(
                name="",
                description="Echo arguments.",
                parameters=(),
            )

        with self.assertRaises(ValueError):
            EchoTool(
                name="echo",
                description="",
                parameters=(),
            )

        with self.assertRaises(TypeError):
            EchoTool(
                name="echo",
                description="Echo arguments.",
                parameters=("not a parameter",),  # type: ignore[arg-type]
            )

        parameter = ToolParameter(
            name="city",
            description="The city to query.",
            type="string",
        )
        with self.assertRaises(ValueError):
            EchoTool(
                name="echo",
                description="Echo arguments.",
                parameters=(parameter, parameter),
            )

    def test_openai_schema_uses_function_calling_format(self) -> None:
        tool = WeatherTool()

        self.assertEqual(
            tool.to_openai_tool(),
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get the current weather for a city.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "city": {
                                "type": "string",
                                "description": "The city to query.",
                            }
                        },
                        "required": ["city"],
                    },
                },
            },
        )

    def test_anthropic_schema_uses_tool_use_format(self) -> None:
        tool = WeatherTool()

        self.assertEqual(
            tool.to_anthropic_tool(),
            {
                "name": "get_weather",
                "description": "Get the current weather for a city.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "city": {
                            "type": "string",
                            "description": "The city to query.",
                        }
                    },
                    "required": ["city"],
                },
            },
        )

    def test_schema_is_generated_from_parameters(self) -> None:
        tool = WeatherTool(parameters=())

        self.assertEqual(
            tool.parameters_schema,
            {"type": "object", "properties": {}},
        )

    def test_default_factory_creates_tools_without_dependencies(self) -> None:
        tool = WeatherTool.create(ToolContext())

        self.assertTrue(WeatherTool.enabled(ToolContext()))
        self.assertIsInstance(tool, WeatherTool)


class ToolExecutionTest(unittest.IsolatedAsyncioTestCase):
    async def test_execute_receives_model_arguments_and_returns_result(self) -> None:
        tool = WeatherTool()

        result = await tool.execute(city="Beijing")

        self.assertEqual(tool.received_arguments, {"city": "Beijing"})
        self.assertEqual(result, ToolResult(content="Beijing: sunny"))
