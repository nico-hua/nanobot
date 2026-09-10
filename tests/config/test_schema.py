"""Tests for MCP-related configuration."""

import unittest

from pydantic import ValidationError

from nanobot.config import (
    ApiConfig,
    AuthConfig,
    MCPServerConfig,
    QQChannelConfig,
    ToolsConfig,
    WebSearchToolConfig,
    WebSocketChannelConfig,
)
from nanobot.config.schema import NanobotConfig, NanobotFileConfig, ProviderConfig


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
        self.assertFalse(config.streaming)
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


class ApiConfigTest(unittest.TestCase):
    def test_defaults_to_a_disabled_local_listener(self) -> None:
        config = ApiConfig()

        self.assertFalse(config.enabled)
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 8000)
        self.assertEqual(config.request_timeout_seconds, 60.0)

    def test_rejects_invalid_listener_settings(self) -> None:
        for values in (
            {"host": " "},
            {"port": -1},
            {"port": 65_536},
            {"request_timeout_seconds": 0},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                ApiConfig(**values)


class AuthConfigTest(unittest.TestCase):
    def test_defaults_to_disabled_with_no_token(self) -> None:
        config = AuthConfig()

        self.assertFalse(config.enabled)
        self.assertEqual(config.token, "")

    def test_normalizes_a_whitespace_only_token(self) -> None:
        self.assertEqual(AuthConfig(enabled=True, token="  ").token, "")


class ToolsConfigTest(unittest.TestCase):
    def test_defaults_to_an_unconfigured_tavily_key(self) -> None:
        config = ToolsConfig()

        self.assertEqual(config.web_search.tavily_api_key, "")

    def test_normalizes_the_tavily_key_without_revealing_it(self) -> None:
        config = WebSearchToolConfig(tavily_api_key="  test-tavily-key  ")

        self.assertEqual(config.tavily_api_key, "test-tavily-key")


class WebSocketChannelConfigTest(unittest.TestCase):
    def test_defaults_to_a_local_listener(self) -> None:
        config = WebSocketChannelConfig()

        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 8765)
        self.assertTrue(config.streaming)

    def test_rejects_invalid_listener_settings(self) -> None:
        for values in (
            {"host": " "},
            {"port": -1},
            {"port": 65_536},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                WebSocketChannelConfig(**values)


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
        self.assertFalse(config.api.enabled)
        self.assertFalse(config.auth.enabled)
        self.assertEqual(config.tools.web_search.tavily_api_key, "")
        self.assertIsNone(config.websocket)

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


class NanobotFileConfigTest(unittest.TestCase):
    def test_groups_non_sensitive_settings_by_runtime_concern(self) -> None:
        config = NanobotFileConfig(
            workspace="workspace",
            agent={
                "context_window_tokens": 512,
                "compaction_threshold_tokens": 192,
                "compaction_recent_tokens": 96,
            },
            cron={"timezone": "UTC"},
            tools={"web_search": {"tavily_api_key": "test-tavily-key"}},
            provider={
                "type": "openai_compat",
                "api_key": "test-key",
                "api_base": "https://example.test/v1",
                "model": "test-model",
            },
            channel={
                "default": "websocket",
                # Non-selected Channel entries are deliberately not parsed.
                "qq": {
                    "app_id": "incomplete-but-not-selected",
                },
                "websocket": {"port": 8101},
            },
            mcp={"servers": {"local": {"command": "python"}}},
        )

        self.assertEqual(config.agent.context_window_tokens, 512)
        self.assertEqual(config.cron.timezone, "UTC")
        self.assertEqual(config.tools.web_search.tavily_api_key, "test-tavily-key")
        self.assertEqual(config.channel.default, "websocket")
        default_channel = config.channel.default_config()
        self.assertIsInstance(default_channel, WebSocketChannelConfig)
        self.assertEqual(default_channel.port, 8101)
        self.assertIn("local", config.mcp.servers)

    def test_validates_only_the_selected_channel_configuration(self) -> None:
        config = NanobotFileConfig(
            workspace="workspace",
            provider={
                "type": "openai_compat",
                "api_key": "test-key",
                "api_base": "https://example.test/v1",
                "model": "test-model",
            },
            channel={
                "default": "qq",
                "qq": {"app_id": "app", "secret": "secret"},
                # This would be invalid if WebSocket were selected.
                "websocket": {"host": ""},
            },
        )

        default_channel = config.channel.default_config()

        self.assertIsInstance(default_channel, QQChannelConfig)
        self.assertEqual(default_channel.app_id, "app")
