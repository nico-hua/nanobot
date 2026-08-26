"""Tests for MCP-related configuration."""

import unittest

from pydantic import ValidationError

from nanobot.config import MCPServerConfig


class MCPServerConfigTest(unittest.TestCase):
    def test_defaults_and_infers_stdio_from_command(self) -> None:
        config = MCPServerConfig(command="python")

        self.assertEqual(config.type, "stdio")
        self.assertEqual(config.args, [])
        self.assertEqual(config.env, {})
        self.assertEqual(config.headers, {})
        self.assertEqual(config.tool_timeout, 30.0)
        self.assertEqual(config.enabled_tools, ["*"])
        self.assertTrue(config.allows_tool("anything"))

    def test_infers_streamable_http_from_url(self) -> None:
        config = MCPServerConfig(url="https://example.test/mcp")

        self.assertEqual(config.type, "streamableHttp")

    def test_accepts_explicit_sse_and_tool_filter(self) -> None:
        config = MCPServerConfig(
            type="sse",
            url="https://example.test/sse",
            enabled_tools=["weather"],
        )

        self.assertTrue(config.allows_tool("weather"))
        self.assertFalse(config.allows_tool("time"))

    def test_rejects_invalid_connection_and_timeout_settings(self) -> None:
        invalid_configs = (
            {},
            {"type": "stdio"},
            {"type": "sse"},
            {"type": "streamableHttp"},
            {"command": "python", "tool_timeout": 0},
            {"command": "python", "tool_timeout": 121},
            {"command": "python", "enabled_tools": ["*", "weather"]},
        )

        for values in invalid_configs:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                MCPServerConfig(**values)
