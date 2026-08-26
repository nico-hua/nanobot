"""Tests for MCP connection management and tool wrapping."""

from __future__ import annotations

import asyncio
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from typing_extensions import Self

from nanobot.config import MCPServerConfig
from nanobot.mcp import MCPProvider, MCPToolWrapper
from nanobot.tools import ToolRegistry


def mcp_tool(
    name: str,
    description: str = "Get the weather.",
    input_schema: dict[str, Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        description=description,
        inputSchema=input_schema
        or {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name."},
            },
            "required": ["city"],
        },
    )


class FakeSession:
    def __init__(
        self,
        tools: list[SimpleNamespace],
        *,
        call_result: Any | None = None,
        list_error: Exception | None = None,
        call_error: Exception | None = None,
    ) -> None:
        self.tools = tools
        self.call_result = call_result or SimpleNamespace(
            content=[SimpleNamespace(type="text", text="sunny")],
            isError=False,
        )
        self.list_error = list_error
        self.call_error = call_error
        self.initialize_calls = 0
        self.list_tools_calls = 0
        self.call_tool_calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *arguments: object) -> None:
        self.closed = True

    async def initialize(self) -> None:
        self.initialize_calls += 1

    async def list_tools(self) -> SimpleNamespace:
        self.list_tools_calls += 1
        if self.list_error is not None:
            raise self.list_error
        return SimpleNamespace(tools=self.tools)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        self.call_tool_calls.append((name, arguments or {}))
        if self.call_error is not None:
            raise self.call_error
        return self.call_result


class SessionFactory:
    def __init__(self, sessions: list[FakeSession]) -> None:
        self._sessions = iter(sessions)

    def __call__(self, read_stream: Any, write_stream: Any) -> FakeSession:
        return next(self._sessions)


@asynccontextmanager
async def fake_transport(*arguments: Any, **keywords: Any):
    yield object(), object()


@asynccontextmanager
async def fake_streamable_http_transport(*arguments: Any, **keywords: Any):
    yield object(), object(), lambda: None


class MCPToolWrapperTest(unittest.IsolatedAsyncioTestCase):
    def test_sanitizes_server_and_tool_names(self) -> None:
        wrapper = MCPToolWrapper(
            "weather server",
            mcp_tool("get-weather"),
            FakeSession([]),
            tool_timeout=1,
        )

        self.assertEqual(wrapper.name, "mcp_weather_server_get_weather")

    async def test_exposes_schema_and_executes_through_registry(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name."},
            },
            "required": ["city"],
        }
        session = FakeSession([mcp_tool("get_weather", input_schema=schema)])
        wrapper = MCPToolWrapper(
            "weather",
            mcp_tool("get_weather", input_schema=schema),
            session,
            tool_timeout=1,
        )
        registry = ToolRegistry((wrapper,))

        result = await registry.execute(wrapper.name, {"city": "Beijing"})

        self.assertEqual(wrapper.name, "mcp_weather_get_weather")
        self.assertEqual(wrapper.description, "Get the weather.")
        self.assertEqual(wrapper.parameters_schema, schema)
        self.assertEqual(result.content, "sunny")
        self.assertEqual(session.call_tool_calls, [("get_weather", {"city": "Beijing"})])

    async def test_converts_mcp_error_and_call_exception_to_tool_errors(self) -> None:
        error_result = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="city unavailable")],
            isError=True,
        )
        error_wrapper = MCPToolWrapper(
            "weather",
            mcp_tool("get_weather"),
            FakeSession([], call_result=error_result),
            tool_timeout=1,
        )
        exception_wrapper = MCPToolWrapper(
            "weather",
            mcp_tool("get_weather"),
            FakeSession([], call_error=RuntimeError("connection lost")),
            tool_timeout=1,
        )

        error_result = await error_wrapper.execute(city="Beijing")
        exception_result = await exception_wrapper.execute(city="Beijing")

        self.assertFalse(error_result.success)
        self.assertIn("city unavailable", error_result.error or "")
        self.assertFalse(exception_result.success)
        self.assertIn("connection lost", exception_result.error or "")

    async def test_times_out_a_slow_mcp_tool_call(self) -> None:
        class SlowSession:
            async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
                await asyncio.sleep(0.05)
                return SimpleNamespace(content=[], isError=False)

        wrapper = MCPToolWrapper(
            "slow",
            mcp_tool("wait"),
            SlowSession(),
            tool_timeout=0.01,
        )

        result = await wrapper.execute(city="Beijing")

        self.assertFalse(result.success)
        self.assertIn("timed out", result.error or "")


class MCPProviderTest(unittest.IsolatedAsyncioTestCase):
    async def test_connects_each_supported_transport(self) -> None:
        cases = (
            (
                MCPServerConfig(command="server", args=["--stdio"]),
                "stdio_client",
            ),
            (
                MCPServerConfig(type="sse", url="https://example.test/sse"),
                "sse_client",
            ),
            (
                MCPServerConfig(url="https://example.test/mcp"),
                "streamable_http_client",
            ),
        )

        for config, transport_name in cases:
            with self.subTest(transport=config.type):
                session = FakeSession([mcp_tool("weather")])
                provider = MCPProvider(
                    ToolRegistry(),
                    {"server": config},
                    session_factory=SessionFactory([session]),
                )
                try:
                    transport = (
                        fake_streamable_http_transport
                        if transport_name == "streamable_http_client"
                        else fake_transport
                    )
                    with patch(
                        f"nanobot.mcp.provider.{transport_name}",
                        transport,
                    ):
                        results = await provider.connect_all()

                    self.assertTrue(results[0].success, results[0].error)
                    self.assertEqual(
                        results[0].tool_names,
                        ("mcp_server_weather",),
                    )
                    self.assertEqual(session.initialize_calls, 1)
                    self.assertEqual(session.list_tools_calls, 1)
                finally:
                    await provider.close()

    async def test_enabled_tools_limits_registration(self) -> None:
        session = FakeSession([mcp_tool("weather"), mcp_tool("time")])
        registry = ToolRegistry()
        provider = MCPProvider(
            registry,
            {
                "server": MCPServerConfig(
                    command="server",
                    enabled_tools=["weather"],
                )
            },
            session_factory=SessionFactory([session]),
        )

        try:
            with patch("nanobot.mcp.provider.stdio_client", fake_transport):
                result = await provider.connect_all()

            self.assertEqual(result[0].tool_names, ("mcp_server_weather",))
            self.assertTrue(registry.has("mcp_server_weather"))
            self.assertFalse(registry.has("mcp_server_time"))
            tool_result = await registry.execute(
                "mcp_server_weather",
                {"city": "Beijing"},
            )
            self.assertEqual(tool_result.content, "sunny")
            self.assertEqual(
                session.call_tool_calls,
                [("weather", {"city": "Beijing"})],
            )
        finally:
            await provider.close()

    async def test_multiple_servers_register_independently_and_close_cleanly(self) -> None:
        alpha_session = FakeSession([mcp_tool("weather")])
        beta_session = FakeSession([mcp_tool("time")])
        registry = ToolRegistry()
        provider = MCPProvider(
            registry,
            {
                "alpha": MCPServerConfig(command="server"),
                "beta": MCPServerConfig(command="server"),
            },
            session_factory=SessionFactory([alpha_session, beta_session]),
        )

        with patch("nanobot.mcp.provider.stdio_client", fake_transport):
            results = await provider.connect_all()

        self.assertEqual(
            tuple(result.tool_names for result in results),
            (("mcp_alpha_weather",), ("mcp_beta_time",)),
        )
        await provider.close()
        self.assertEqual(registry.tools, ())
        self.assertTrue(alpha_session.closed)
        self.assertTrue(beta_session.closed)

    async def test_one_server_failure_does_not_stop_other_servers(self) -> None:
        failing_session = FakeSession([], list_error=RuntimeError("unavailable"))
        working_session = FakeSession([mcp_tool("weather")])
        registry = ToolRegistry()
        provider = MCPProvider(
            registry,
            {
                "bad": MCPServerConfig(command="server"),
                "good": MCPServerConfig(command="server"),
            },
            session_factory=SessionFactory([failing_session, working_session]),
        )

        try:
            with patch("nanobot.mcp.provider.stdio_client", fake_transport):
                results = await provider.connect_all()

            self.assertFalse(results[0].success)
            self.assertIn("unavailable", results[0].error or "")
            self.assertTrue(results[1].success)
            self.assertTrue(registry.has("mcp_good_weather"))
            self.assertTrue(failing_session.closed)
        finally:
            await provider.close()
