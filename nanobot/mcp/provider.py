"""Connection lifecycle management for MCP-backed tools."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from ..config import MCPServerConfig
from ..tools import ToolRegistry
from .tool import MCPToolWrapper


@dataclass(frozen=True)
class MCPConnectionResult:
    """The tools registered for one server, or a connection error."""

    server_name: str
    tool_names: tuple[str, ...] = ()
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.error is None


@dataclass
class _MCPConnection:
    stack: AsyncExitStack
    tools: dict[str, MCPToolWrapper]


class MCPProvider:
    """Connect configured MCP servers and dynamically register their tools."""

    def __init__(
        self,
        registry: ToolRegistry,
        servers: Mapping[str, MCPServerConfig],
        *,
        session_factory: Callable[[Any, Any], Any] = ClientSession,
    ) -> None:
        if not isinstance(registry, ToolRegistry):
            raise TypeError("MCPProvider requires a ToolRegistry")

        self._registry = registry
        self._server_configs = _server_configs(servers)
        self._session_factory = session_factory
        self._connections: dict[str, _MCPConnection] = {}
        self.errors: dict[str, str] = {}

    async def connect_all(self) -> tuple[MCPConnectionResult, ...]:
        """Connect each configured server without letting one failure stop others."""

        results: list[MCPConnectionResult] = []
        for server_name in sorted(self._server_configs):
            results.append(await self.connect_server(server_name))
        return tuple(results)

    async def connect_server(self, server_name: str) -> MCPConnectionResult:
        """Connect one server, initialize it, and register its enabled tools."""

        config = self._server_configs.get(server_name)
        if config is None:
            return MCPConnectionResult(
                server_name=server_name,
                error=f"Unknown MCP server: {server_name}",
            )
        if server_name in self._connections:
            return MCPConnectionResult(
                server_name=server_name,
                error=f"MCP server is already connected: {server_name}",
            )

        stack = AsyncExitStack()
        registered_tools: dict[str, MCPToolWrapper] = {}
        try:
            read_stream, write_stream = await stack.enter_async_context(
                self._open_transport(config)
            )
            session = await stack.enter_async_context(
                self._session_factory(read_stream, write_stream)
            )
            await session.initialize()
            tool_list = await session.list_tools()

            for mcp_tool in tool_list.tools:
                if not config.allows_tool(mcp_tool.name):
                    continue

                tool = MCPToolWrapper(
                    server_name,
                    mcp_tool,
                    session,
                    config.tool_timeout,
                )
                if self._registry.has(tool.name):
                    raise ValueError(f"MCP tool name is already registered: {tool.name}")
                self._registry.register(tool)
                registered_tools[tool.name] = tool
        except Exception as exc:  # noqa: BLE001
            for tool_name, tool in registered_tools.items():
                if self._registry.get(tool_name) is tool:
                    self._registry.remove(tool_name)
            await stack.aclose()
            error = f"Unable to connect MCP server {server_name}: {exc}"
            self.errors[server_name] = error
            return MCPConnectionResult(server_name=server_name, error=error)

        self._connections[server_name] = _MCPConnection(stack, registered_tools)
        self.errors.pop(server_name, None)
        return MCPConnectionResult(
            server_name=server_name,
            tool_names=tuple(registered_tools),
        )

    async def close(self) -> None:
        """Unregister MCP tools and close all live MCP connections."""

        for server_name in tuple(self._connections):
            await self.close_server(server_name)

    async def close_server(self, server_name: str) -> None:
        """Unregister one server's tools and release its session and transport."""

        connection = self._connections.pop(server_name, None)
        if connection is None:
            return

        for tool_name, tool in connection.tools.items():
            if self._registry.get(tool_name) is tool:
                self._registry.remove(tool_name)
        await connection.stack.aclose()

    @asynccontextmanager
    async def _open_transport(
        self,
        config: MCPServerConfig,
    ) -> AsyncIterator[tuple[Any, Any]]:
        if config.type == "stdio":
            assert config.command is not None
            parameters = StdioServerParameters(
                command=config.command,
                args=config.args,
                env=config.env or None,
                cwd=config.cwd,
            )
            async with stdio_client(parameters) as streams:
                yield streams
            return

        if config.type == "sse":
            assert config.url is not None
            async with sse_client(config.url, headers=config.headers or None) as streams:
                yield streams
            return

        assert config.type == "streamableHttp"
        assert config.url is not None
        async with httpx.AsyncClient(headers=config.headers) as http_client:  # noqa: SIM117
            async with streamable_http_client(
                config.url,
                http_client=http_client,
            ) as (read_stream, write_stream, _):
                yield read_stream, write_stream


def _server_configs(
    servers: Mapping[str, MCPServerConfig],
) -> dict[str, MCPServerConfig]:
    if not isinstance(servers, Mapping):
        raise TypeError("MCPProvider servers must be a mapping")
    if not all(
        isinstance(name, str) and isinstance(config, MCPServerConfig)
        for name, config in servers.items()
    ):
        raise TypeError("MCPProvider servers must map names to MCPServerConfig")
    return dict(servers)
