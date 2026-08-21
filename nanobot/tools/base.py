"""Base types for tools that can be called by an agent."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

ToolParameterType = Literal["string", "integer", "number", "boolean"]
_TOOL_PARAMETER_TYPES = frozenset({"string", "integer", "number", "boolean"})


@dataclass(frozen=True)
class ToolResult:
    """The normalized result of executing a tool."""

    content: str
    success: bool = True
    error: str | None = None


@dataclass(frozen=True)
class ToolParameter:
    """A single scalar parameter accepted by a tool."""

    name: str
    description: str
    type: ToolParameterType
    required: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Tool parameter name must not be empty")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("Tool parameter description must not be empty")
        if self.type not in _TOOL_PARAMETER_TYPES:
            raise ValueError(f"Unsupported tool parameter type: {self.type}")
        if not isinstance(self.required, bool):
            raise TypeError("Tool parameter required must be a boolean")


class Tool(ABC):
    """Base class for a callable tool and its provider schemas."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: Sequence[ToolParameter] = (),
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Tool name must not be empty")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("Tool description must not be empty")
        if not isinstance(parameters, Sequence):
            raise TypeError("Tool parameters must be a sequence")
        if not all(isinstance(parameter, ToolParameter) for parameter in parameters):
            raise TypeError("Tool parameters must contain ToolParameter instances")

        parameter_names = [parameter.name for parameter in parameters]
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError("Tool parameter names must be unique")

        self.name = name
        self.description = description
        self.parameters = tuple(parameters)

    @property
    def parameters_schema(self) -> dict[str, Any]:
        """Return the JSON Schema generated from this tool's parameters."""

        schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                parameter.name: {
                    "type": parameter.type,
                    "description": parameter.description,
                }
                for parameter in self.parameters
            },
        }
        required = [
            parameter.name for parameter in self.parameters if parameter.required
        ]
        if required:
            schema["required"] = required
        return schema

    @abstractmethod
    async def execute(self, **arguments: Any) -> ToolResult:
        """Execute the tool with the arguments requested by the model."""

        raise NotImplementedError

    def to_openai_tool(self) -> dict[str, Any]:
        """Return this tool in OpenAI function-calling format."""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }

    def to_anthropic_tool(self) -> dict[str, Any]:
        """Return this tool in Anthropic tool-use format."""

        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters_schema,
        }
