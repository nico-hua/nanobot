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
                        "context_window_tokens": 512,
                        "compaction_threshold_tokens": 192,
                        "compaction_recent_tokens": 96,
                        "logging": {"level": "DEBUG"},
                        "provider": {
                            "type": "openai_compat",
                            "api_base": "https://example.test/v1",
                            "model": "test-model",
                            "max_tokens": 64,
                            "temperature": 0.3,
                        },
                        "default_channel": "qq",
                        "mcp_servers": {},
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
