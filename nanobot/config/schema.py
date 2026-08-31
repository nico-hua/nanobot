"""Configuration models for tools, MCP servers, and channels."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

MCPTransportType = Literal["stdio", "sse", "streamableHttp"]
ProviderType = Literal["openai_compat", "anthropic_compat"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class LoggingConfig(BaseModel):
    """Non-sensitive logging settings stored in ``nanobot.json``."""

    model_config = ConfigDict(extra="forbid")

    level: LogLevel = "INFO"


class ProviderSettingsConfig(BaseModel):
    """Non-sensitive provider settings stored in ``nanobot.json``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: ProviderType
    api_base: str
    default_model: str = Field(
        validation_alias=AliasChoices("model", "default_model"),
    )
    default_max_tokens: int = Field(
        default=1024,
        gt=0,
        validation_alias=AliasChoices("max_tokens", "default_max_tokens"),
    )
    default_temperature: float = Field(
        default=0.7,
        ge=0,
        le=2,
        validation_alias=AliasChoices("temperature", "default_temperature"),
    )

    @field_validator("api_base", "default_model")
    @classmethod
    def _reject_blank_values(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class ProviderConfig(ProviderSettingsConfig):
    """Resolved provider configuration, including the API key from ``.env``."""

    api_key: str

    @field_validator("api_key")
    @classmethod
    def _reject_blank_api_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class QQChannelConfig(BaseModel):
    """Credentials and sender allow-list for one QQ channel."""

    model_config = ConfigDict(extra="forbid")

    app_id: str
    secret: str
    enabled: bool = True
    allow_from: list[str] = Field(default_factory=lambda: ["*"])

    @field_validator("app_id", "secret")
    @classmethod
    def _reject_blank_credentials(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("allow_from")
    @classmethod
    def _validate_allow_from(cls, value: list[str]) -> list[str]:
        if any(not sender_id.strip() for sender_id in value):
            raise ValueError("sender IDs must not be blank")
        if "*" in value and value != ["*"]:
            raise ValueError('"*" must be the only allow_from selector')
        return value

    def allows_sender(self, sender_id: str) -> bool:
        """Return whether this sender is allowed to use the QQ channel."""

        return self.allow_from == ["*"] or sender_id in self.allow_from


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


class NanobotFileConfig(BaseModel):
    """Non-sensitive configuration loaded from ``.nanobot/nanobot.json``."""

    model_config = ConfigDict(extra="forbid")

    workspace: Path
    max_history_tokens: int = Field(default=64_000, ge=0)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    provider: ProviderSettingsConfig
    default_channel: str = "qq"
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)

    @field_validator("default_channel")
    @classmethod
    def _reject_blank_default_channel(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class NanobotConfig(BaseModel):
    """Resolved runtime configuration after merging file and secret settings."""

    model_config = ConfigDict(extra="forbid")

    provider: ProviderConfig
    workspace: Path | None = None
    max_history_tokens: int = Field(default=64_000, ge=0)
    default_channel: str = "qq"
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    qq: QQChannelConfig | None = None
