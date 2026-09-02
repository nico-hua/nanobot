"""Tests for MCP-related configuration."""

import unittest

from pydantic import ValidationError

from nanobot.config import MCPServerConfig, QQChannelConfig
from nanobot.config.schema import NanobotConfig, ProviderConfig


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


class QQChannelConfigTest(unittest.TestCase):
    def test_defaults_to_allowing_all_senders(self) -> None:
        config = QQChannelConfig(app_id="app", secret="secret")

        self.assertEqual(config.allow_from, ["*"])
        self.assertTrue(config.allows_sender("user-1"))

    def test_filters_senders_and_rejects_invalid_values(self) -> None:
        config = QQChannelConfig(
            app_id="app",
            secret="secret",
            allow_from=["user-1"],
        )

        self.assertTrue(config.allows_sender("user-1"))
        self.assertFalse(config.allows_sender("user-2"))
        with self.assertRaises(ValidationError):
            QQChannelConfig(app_id="", secret="secret")
        with self.assertRaises(ValidationError):
            QQChannelConfig(app_id="app", secret="secret", allow_from=["*", "user-1"])


class ProviderConfigTest(unittest.TestCase):
    def test_defaults_generation_settings(self) -> None:
        config = ProviderConfig(
            type="openai_compat",
            api_key="test-key",
            api_base="https://example.test/v1",
            default_model="test-model",
        )

        self.assertEqual(config.default_max_tokens, 1024)
        self.assertEqual(config.default_temperature, 0.7)

    def test_rejects_temperature_outside_supported_range(self) -> None:
        with self.assertRaises(ValidationError):
            ProviderConfig(
                type="openai_compat",
                api_key="test-key",
                api_base="https://example.test/v1",
                default_model="test-model",
                default_temperature=2.1,
            )


class NanobotConfigTest(unittest.TestCase):
    def test_defaults_to_qq_as_the_selected_channel(self) -> None:
        config = NanobotConfig(
            provider=ProviderConfig(
                type="openai_compat",
                api_key="test-key",
                api_base="https://example.test/v1",
                default_model="test-model",
            )
        )

        self.assertEqual(config.default_channel, "qq")
        self.assertEqual(config.context_window_tokens, 128_000)
        self.assertEqual(config.compaction_threshold_tokens, 64_000)
        self.assertEqual(config.compaction_recent_tokens, 32_000)
        self.assertEqual(config.cron_timezone, "Asia/Shanghai")

    def test_rejects_an_invalid_cron_timezone(self) -> None:
        with self.assertRaises(ValidationError):
            NanobotConfig(
                provider=ProviderConfig(
                    type="openai_compat",
                    api_key="test-key",
                    api_base="https://example.test/v1",
                    default_model="test-model",
                ),
                cron_timezone="not/a-timezone",
            )

    def test_rejects_a_non_positive_context_window(self) -> None:
        with self.assertRaises(ValidationError):
            NanobotConfig(
                provider=ProviderConfig(
                    type="openai_compat",
                    api_key="test-key",
                    api_base="https://example.test/v1",
                    default_model="test-model",
                ),
                context_window_tokens=0,
            )

    def test_rejects_an_invalid_compaction_budget_relationship(self) -> None:
        with self.assertRaises(ValidationError):
            NanobotConfig(
                provider=ProviderConfig(
                    type="openai_compat",
                    api_key="test-key",
                    api_base="https://example.test/v1",
                    default_model="test-model",
                ),
                compaction_threshold_tokens=100,
                compaction_recent_tokens=100,
            )
