"""Local stdio integration test using a real MCP SDK server."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from nanobot.config import MCPServerConfig
from nanobot.mcp import MCPProvider
from nanobot.tools import ToolRegistry


class RealMCPStdioIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_registers_and_executes_a_real_mcp_tool(self) -> None:
        registry = ToolRegistry()
        provider = MCPProvider(
            registry,
            {
                "local_math": MCPServerConfig(
                    command=sys.executable,
                    args=["-B", "-m", "tests.mcp_tests.real_server"],
                    cwd=Path(__file__).parents[2],
                    tool_timeout=5,
                )
            },
        )

        try:
            connection = (await provider.connect_all())[0]

            self.assertTrue(connection.success, connection.error)
            self.assertEqual(
                connection.tool_names,
                ("mcp_local_math_add_numbers",),
            )
            result = await registry.execute(
                "mcp_local_math_add_numbers",
                {"left": 7, "right": 5},
            )

            self.assertTrue(result.success)
            self.assertEqual(result.content, "12")
        finally:
            await provider.close()
