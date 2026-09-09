"""Tests for JSON-only runtime configuration loading."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nanobot.config import ConfigError, load_file_config, load_nanobot_config


class ConfigLoaderTest(unittest.TestCase):
    def test_loads_json_settings_and_resolves_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "nanobot.json"
            config_path.write_text(
                json.dumps(
                    {
                        "workspace": "workspace",
                        "agent": {
                            "context_window_tokens": 512,
                            "compaction_threshold_tokens": 192,
                            "compaction_recent_tokens": 96,
                        },
                        "cron": {"timezone": "UTC"},
                        "logging": {"level": "DEBUG"},
                        "api": {
                            "enabled": True,
                            "host": "127.0.0.1",
                            "port": 8100,
                            "request_timeout_seconds": 15,
                        },
                        "channel": {
                            "default": "qq",
                            "qq": {
                                "app_id": "test-app-id",
                                "secret": "test-secret",
                                "allow_from": ["user-1", "user-2"],
                                "streaming": True,
                            },
                            "websocket": {
                                "host": "127.0.0.1",
                                "port": 8101,
                            },
                        },
                        "provider": {
                            "type": "openai_compat",
                            "api_key": "test-key",
                            "api_base": "https://example.test/v1",
                            "model": "test-model",
                            "max_tokens": 64,
                            "temperature": 0.3,
                        },
                        "mcp": {
                            "servers": {
                                "local": {"command": "python"},
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            file_config = load_file_config(config_path)
            config = load_nanobot_config(config_path)

        self.assertEqual(config.workspace, config_path.parent / "workspace")
        self.assertEqual(config.context_window_tokens, 512)
        self.assertEqual(config.compaction_threshold_tokens, 192)
        self.assertEqual(config.compaction_recent_tokens, 96)
        self.assertEqual(config.cron_timezone, "UTC")
        self.assertTrue(config.api.enabled)
        self.assertEqual(config.api.port, 8100)
        self.assertEqual(config.api.request_timeout_seconds, 15)
        self.assertIsNone(config.websocket)
        self.assertIn("local", config.mcp_servers)
        self.assertEqual(file_config.provider.api_key, "test-key")
        self.assertEqual(config.provider.api_key, "test-key")
        self.assertEqual(config.provider.default_model, "test-model")
        self.assertEqual(config.provider.default_max_tokens, 64)
        self.assertEqual(config.provider.default_temperature, 0.3)
        self.assertIsNotNone(config.qq)
        self.assertEqual(config.qq.allow_from if config.qq else None, ["user-1", "user-2"])
        self.assertTrue(config.qq.streaming if config.qq else False)

    def test_enabled_auth_generates_and_persists_one_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            config_path = _write_file_config(directory_path)
            raw_config = json.loads(config_path.read_text(encoding="utf-8"))
            raw_config["auth"] = {"enabled": True, "token": ""}
            config_path.write_text(json.dumps(raw_config), encoding="utf-8")

            with patch(
                "nanobot.config.loader.secrets.token_urlsafe",
                return_value="generated-static-token",
            ) as generate_token:
                first_config = load_nanobot_config(config_path)

            persisted = json.loads(config_path.read_text(encoding="utf-8"))
            second_config = load_nanobot_config(config_path)

        self.assertTrue(first_config.auth.enabled)
        self.assertEqual(first_config.auth.token, "generated-static-token")
        self.assertEqual(persisted["auth"]["token"], "generated-static-token")
        self.assertEqual(second_config.auth.token, "generated-static-token")
        generate_token.assert_called_once_with(32)

    def test_disabled_auth_does_not_generate_a_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = _write_file_config(Path(directory))

            with patch("nanobot.config.loader.secrets.token_urlsafe") as generate_token:
                config = load_nanobot_config(config_path)

        self.assertFalse(config.auth.enabled)
        self.assertEqual(config.auth.token, "")
        generate_token.assert_not_called()

    def test_rejects_missing_provider_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = _write_file_config(Path(directory), api_key=None)

            with self.assertRaisesRegex(ConfigError, "expected schema"):
                load_nanobot_config(config_path)

    def test_loads_only_the_selected_channel_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = _write_file_config(Path(directory))
            raw_config = json.loads(config_path.read_text(encoding="utf-8"))
            raw_config["channel"] = {
                "default": "websocket",
                # This is intentionally incomplete.  It is not the selected
                # channel, so it must not block a WebSocket-only runtime.
                "qq": {"app_id": "not-used"},
                "websocket": {"port": 8101},
            }
            config_path.write_text(json.dumps(raw_config), encoding="utf-8")

            config = load_nanobot_config(config_path)

        self.assertIsNone(config.qq)
        self.assertIsNotNone(config.websocket)
        self.assertEqual(config.websocket.port if config.websocket else None, 8101)

    def test_rejects_incomplete_selected_qq_json_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = _write_file_config(Path(directory))
            raw_config = json.loads(config_path.read_text(encoding="utf-8"))
            raw_config["channel"] = {
                "default": "qq",
                "qq": {"app_id": "configured-app-id"},
            }
            config_path.write_text(json.dumps(raw_config), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "expected schema"):
                load_nanobot_config(config_path)


def _write_file_config(
    directory: Path,
    *,
    api_key: str | None = "test-key",
) -> Path:
    config_path = directory / "nanobot.json"
    provider: dict[str, str] = {
        "type": "openai_compat",
        "api_base": "https://example.test/v1",
        "model": "test-model",
    }
    if api_key is not None:
        provider["api_key"] = api_key

    config_path.write_text(
        json.dumps(
            {
                "workspace": "workspace",
                "provider": provider,
                "channel": {"default": "websocket"},
            }
        ),
        encoding="utf-8",
    )
    return config_path
