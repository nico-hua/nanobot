"""Adapter from one MCP tool definition to the local Tool interface."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from ..tools import Tool, ToolParameter, ToolResult

_SUPPORTED_PARAMETER_TYPES = frozenset({"string", "integer", "number", "boolean"})


class MCPToolWrapper(Tool):
    """Invoke an MCP tool through a live MCP client session."""

    def __init__(
        self,
        server_name: str,
        mcp_tool: Any,
        session: Any,
        tool_timeout: float,
    ) -> None:
        tool_name = _required_tool_name(mcp_tool)
        input_schema = _input_schema(mcp_tool)
        self.server_name = server_name
        self.mcp_tool_name = tool_name
        self._session = session
        self._tool_timeout = tool_timeout
        self._input_schema = input_schema

        super().__init__(
            name=_wrapped_tool_name(server_name, tool_name),
            description=_tool_description(mcp_tool, tool_name),
            parameters=_parameters_from_schema(input_schema),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        """Return the MCP tool's original input schema unchanged."""

        return deepcopy(self._input_schema)

    async def execute(self, **arguments: Any) -> ToolResult:
        """Call the MCP tool and convert its text response to ToolResult."""

        try:
            result = await asyncio.wait_for(
                self._session.call_tool(self.mcp_tool_name, arguments=arguments),
                timeout=self._tool_timeout,
            )
        except TimeoutError:
            return _tool_error(
                f"MCP tool timed out after {self._tool_timeout:g} seconds: {self.name}"
            )
        except Exception as exc:  # noqa: BLE001
            return _tool_error(f"MCP tool call failed: {self.name} ({exc})")

        content = _text_content(result)
        if getattr(result, "isError", False):
            return _tool_error(f"MCP tool returned an error: {self.name} ({content})")
        return ToolResult(content=content)


def _required_tool_name(mcp_tool: Any) -> str:
    name = getattr(mcp_tool, "name", None)
    if not isinstance(name, str) or not name.strip():
        raise ValueError("MCP tool name must be a non-empty string")
    return name


def _input_schema(mcp_tool: Any) -> dict[str, Any]:
    input_schema = getattr(mcp_tool, "inputSchema", None)
    if not isinstance(input_schema, Mapping):
        raise ValueError("MCP tool inputSchema must be an object")  # noqa: TRY004
    return deepcopy(dict(input_schema))


def _tool_description(mcp_tool: Any, tool_name: str) -> str:
    description = getattr(mcp_tool, "description", None)
    if isinstance(description, str) and description.strip():
        return description
    return f"MCP tool {tool_name}."


def _parameters_from_schema(input_schema: Mapping[str, Any]) -> tuple[ToolParameter, ...]:
    properties = input_schema.get("properties")
    if not isinstance(properties, Mapping):
        return ()

    required = input_schema.get("required", ())
    required_names = set(required) if isinstance(required, list) else set()
    parameters: list[ToolParameter] = []
    for name, definition in properties.items():
        if not isinstance(name, str) or not isinstance(definition, Mapping):
            continue
        parameter_type = definition.get("type")
        if parameter_type not in _SUPPORTED_PARAMETER_TYPES:
            continue
        description = definition.get("description")
        parameters.append(
            ToolParameter(
                name=name,
                description=description
                if isinstance(description, str) and description.strip()
                else f"MCP parameter {name}.",
                type=parameter_type,
                required=name in required_names,
            )
        )
    return tuple(parameters)


def _wrapped_tool_name(server_name: str, tool_name: str) -> str:
    return f"mcp_{_sanitize_component(server_name)}_{_sanitize_component(tool_name)}"


def _sanitize_component(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower())
    return normalized.strip("_") or "tool"


def _text_content(result: Any) -> str:
    text_parts = [
        item.text
        for item in getattr(result, "content", ())
        if getattr(item, "type", None) == "text"
        and isinstance(getattr(item, "text", None), str)
    ]
    return "\n".join(text_parts) or "(empty)"


def _tool_error(message: str) -> ToolResult:
    return ToolResult(content=f"Error: {message}", success=False, error=message)
