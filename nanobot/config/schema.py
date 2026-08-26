"""Configuration models for tools and MCP servers."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MCPTransportType = Literal["stdio", "sse", "streamableHttp"]


class MCPServerConfig(BaseModel):
    """Connection and tool registration settings for one MCP server."""

    model_config = ConfigDict(extra="forbid")

    type: MCPTransportType | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | Path | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    tool_timeout: float = Field(default=30.0, gt=0, le=120)
    enabled_tools: list[str] = Field(default_factory=lambda: ["*"])

    @field_validator("command", "url")
    @classmethod
    def _reject_blank_connection_values(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("enabled_tools")
    @classmethod
    def _validate_enabled_tools(cls, value: list[str]) -> list[str]:
        if any(not name.strip() for name in value):
            raise ValueError("tool names must not be blank")
        if "*" in value and value != ["*"]:
            raise ValueError('"*" must be the only enabled tool selector')
        return value

    @model_validator(mode="after")
    def _resolve_transport(self) -> MCPServerConfig:
        if self.type is None:
            if self.command is not None:
                self.type = "stdio"
            elif self.url is not None:
                self.type = "streamableHttp"
            else:
                raise ValueError("MCP server requires either command or url")

        if self.type == "stdio" and self.command is None:
            raise ValueError("stdio MCP server requires command")
        if self.type in {"sse", "streamableHttp"} and self.url is None:
            raise ValueError(f"{self.type} MCP server requires url")
        return self

    def allows_tool(self, tool_name: str) -> bool:
        """Return whether a server tool should be registered."""

        return self.enabled_tools == ["*"] or tool_name in self.enabled_tools
