"""Tests for loading public JSON settings and private environment values."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nanobot.config import ConfigError, load_file_config, load_nanobot_config


class ConfigLoaderTest(unittest.TestCase):
    def test_merges_json_settings_with_api_key_and_resolves_workspace(self) -> None:
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
                            "websocket": {
                                "host": "127.0.0.1",
                                "port": 8101,
                            },
                        },
                        "provider": {
                            "type": "openai_compat",
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
            env_path = Path(directory) / ".env"
            env_path.write_text("NANOBOT_API_KEY=test-key\n", encoding="utf-8")

            config = load_nanobot_config(config_path, env_path)

        self.assertEqual(config.workspace, config_path.parent / "workspace")
        self.assertEqual(config.context_window_tokens, 512)
        self.assertEqual(config.compaction_threshold_tokens, 192)
        self.assertEqual(config.compaction_recent_tokens, 96)
        self.assertEqual(config.cron_timezone, "UTC")
        self.assertTrue(config.api.enabled)
        self.assertEqual(config.api.port, 8100)
        self.assertEqual(config.api.request_timeout_seconds, 15)
        self.assertEqual(config.websocket.port, 8101)
        self.assertIn("local", config.mcp_servers)
        self.assertEqual(config.provider.api_key, "test-key")
        self.assertEqual(config.provider.default_model, "test-model")
        self.assertEqual(config.provider.default_max_tokens, 64)
        self.assertEqual(config.provider.default_temperature, 0.3)

    def test_process_environment_overrides_dotenv_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = _write_file_config(Path(directory))
            env_path = Path(directory) / ".env"
            env_path.write_text("NANOBOT_API_KEY=file-key\n", encoding="utf-8")

            with patch.dict(os.environ, {"NANOBOT_API_KEY": "process-key"}, clear=True):
                config = load_nanobot_config(config_path, env_path)

        self.assertEqual(config.provider.api_key, "process-key")

    def test_loads_optional_qq_credentials_from_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = _write_file_config(Path(directory))
            file_config = json.loads(config_path.read_text(encoding="utf-8"))
            file_config["channel"] = {"qq": {"streaming": True}}
            config_path.write_text(json.dumps(file_config), encoding="utf-8")
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "\n".join(
                    (
                        "NANOBOT_API_KEY=test-key",
                        "NANOBOT_QQ_APP_ID=test-app-id",
                        "NANOBOT_QQ_SECRET=test-secret",
                        "NANOBOT_QQ_ALLOW_FROM=user-1, user-2",
                    )
                ),
                encoding="utf-8",
            )

            config = load_nanobot_config(config_path, env_path)

        self.assertIsNotNone(config.qq)
        self.assertEqual(config.qq.app_id if config.qq else None, "test-app-id")
        self.assertEqual(config.qq.allow_from if config.qq else None, ["user-1", "user-2"])
        self.assertTrue(config.qq.streaming if config.qq else False)

    def test_rejects_incomplete_qq_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = _write_file_config(Path(directory))
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "NANOBOT_API_KEY=test-key\nNANOBOT_QQ_APP_ID=test-app-id\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "QQ configuration"):
                load_nanobot_config(config_path, env_path)

    def test_rejects_missing_api_key_and_sensitive_json_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            config_path = _write_file_config(directory_path)
            env_path = directory_path / ".env"
            env_path.write_text("", encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "NANOBOT_API_KEY"):
                load_nanobot_config(config_path, env_path)

            sensitive_path = directory_path / "sensitive.json"
            sensitive_path.write_text(
                json.dumps(
                    {
                        "workspace": "workspace",
                        "provider": {
                            "type": "openai_compat",
                            "api_base": "https://example.test/v1",
                            "model": "test-model",
                            "api_key": "not-allowed",
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_file_config(sensitive_path)


def _write_file_config(directory: Path) -> Path:
    config_path = directory / "nanobot.json"
    config_path.write_text(
        json.dumps(
            {
                "workspace": "workspace",
                "provider": {
                    "type": "openai_compat",
                    "api_base": "https://example.test/v1",
                    "model": "test-model",
                },
            }
        ),
        encoding="utf-8",
    )
    return config_path
