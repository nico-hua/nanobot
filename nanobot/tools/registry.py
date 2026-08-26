"""Registration and execution of tools available to an agent."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .base import Tool, ToolParameter, ToolResult


class ToolRegistry:
    """Keep an ordered collection of tools and execute validated calls."""

    def __init__(self, tools: Sequence[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    @property
    def tools(self) -> tuple[Tool, ...]:
        """Return registered tools in their registration order."""

        return tuple(self._tools.values())

    @property
    def schemas(self) -> tuple[dict[str, Any], ...]:
        """Return provider-neutral schemas for all registered tools."""

        return tuple(
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters_schema,
            }
            for tool in self._tools.values()
        )

    def register(self, tool: Tool) -> None:
        """Register one tool, rejecting duplicate names explicitly."""

        if not isinstance(tool, Tool):
            raise TypeError("Only Tool instances can be registered")
        if tool.name in self._tools:
            raise ValueError(f"Tool is already registered: {tool.name}")
        self._tools[tool.name] = tool

    def remove(self, name: str) -> Tool | None:
        """Remove and return a tool, or return None when it is not registered."""

        return self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Return the tool registered under a name, if present."""

        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Return whether a tool name is registered."""

        return name in self._tools

    async def execute(
        self,
        name: str,
        arguments: Mapping[str, Any],
    ) -> ToolResult:
        """Validate model arguments and execute the matching registered tool."""

        tool = self.get(name)
        if tool is None:
            return _tool_error(f"Unknown tool: {name}")

        validated_arguments = _validate_arguments(tool, arguments)
        if isinstance(validated_arguments, ToolResult):
            return validated_arguments

        try:
            result = await tool.execute(**validated_arguments)
        except Exception as exc:  # noqa: BLE001
            return _tool_error(f"Tool execution failed: {name} ({exc})")

        if not isinstance(result, ToolResult):
            return _tool_error(f"Tool returned an invalid result: {name}")
        return result


def _validate_arguments(
    tool: Tool,
    arguments: Mapping[str, Any],
) -> dict[str, Any] | ToolResult:
    if not isinstance(arguments, Mapping):
        return _tool_error(f"Tool arguments for {tool.name} must be an object")

    parameter_names = {parameter.name for parameter in tool.parameters}
    unexpected_names = sorted(set(arguments) - parameter_names)
    if unexpected_names:
        return _tool_error(
            f"Unknown parameter for {tool.name}: {unexpected_names[0]}"
        )

    validated_arguments: dict[str, Any] = {}
    for parameter in tool.parameters:
        if parameter.name not in arguments:
            if parameter.required:
                return _tool_error(
                    f"Missing required parameter for {tool.name}: {parameter.name}"
                )
            continue

        value = arguments[parameter.name]
        if not _matches_parameter_type(value, parameter):
            return _tool_error(
                f"Invalid {parameter.type} parameter for {tool.name}: "
                f"{parameter.name}"
            )
        validated_arguments[parameter.name] = value

    return validated_arguments


def _matches_parameter_type(value: Any, parameter: ToolParameter) -> bool:
    if parameter.type == "string":
        return isinstance(value, str)
    if parameter.type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if parameter.type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, bool)


def _tool_error(message: str) -> ToolResult:
    return ToolResult(content=f"Error: {message}", success=False, error=message)
