"""Concrete tools used by tests outside the tools package."""

from typing import Any

from nanobot.tools import Tool, ToolParameter, ToolResult


class WeatherTool(Tool):
    def __init__(
        self,
        parameters: tuple[ToolParameter, ...] | None = None,
    ) -> None:
        super().__init__(
            name="get_weather",
            description="Get the current weather for a city.",
            parameters=parameters
            if parameters is not None
            else (
                ToolParameter(
                    name="city",
                    description="The city to query.",
                    type="string",
                    required=True,
                ),
            ),
        )
        self.received_arguments: dict[str, Any] | None = None

    async def execute(self, **arguments: Any) -> ToolResult:
        self.received_arguments = arguments
        return ToolResult(content=f"{arguments['city']}: sunny")
