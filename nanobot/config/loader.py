"""Load non-sensitive JSON settings and merge secrets from ``.env``."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import ValidationError

from .schema import (
    NanobotConfig,
    NanobotFileConfig,
    ProviderConfig,
    QQChannelConfig,
)

DEFAULT_CONFIG_PATH = Path(".nanobot/nanobot.json")
DEFAULT_ENV_PATH = Path(".env")
_API_KEY_ENV_VAR = "NANOBOT_API_KEY"
_QQ_APP_ID_ENV_VAR = "NANOBOT_QQ_APP_ID"
_QQ_SECRET_ENV_VAR = "NANOBOT_QQ_SECRET"
_QQ_ALLOW_FROM_ENV_VAR = "NANOBOT_QQ_ALLOW_FROM"


class ConfigError(ValueError):
    """Raised when configuration cannot be loaded or merged safely."""


def load_file_config(
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> NanobotFileConfig:
    """Load and validate the non-sensitive JSON configuration file."""

    path = Path(config_path)
    try:
        raw_config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file was not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"Configuration file could not be read: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Configuration file contains invalid JSON: {path}") from exc

    if not isinstance(raw_config, dict):
        raise ConfigError("Configuration file root must be a JSON object")

    try:
        return NanobotFileConfig.model_validate(raw_config)
    except ValidationError as exc:
        raise ConfigError("Configuration file does not match the expected schema") from exc


def load_nanobot_config(
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    env_path: str | Path = DEFAULT_ENV_PATH,
) -> NanobotConfig:
    """Merge file settings with the provider API key from the environment."""

    path = Path(config_path)
    file_config = load_file_config(path)
    api_key = get_env_value(_API_KEY_ENV_VAR, env_path)
    if not api_key:
        raise ConfigError(f"Missing required environment variable: {_API_KEY_ENV_VAR}")

    workspace = file_config.workspace
    if not workspace.is_absolute():
        workspace = (path.parent / workspace).resolve()

    return NanobotConfig(
        workspace=workspace,
        context_window_tokens=file_config.context_window_tokens,
        compaction_threshold_tokens=file_config.compaction_threshold_tokens,
        compaction_recent_tokens=file_config.compaction_recent_tokens,
        cron_timezone=file_config.cron_timezone,
        api=file_config.api,
        default_channel=file_config.default_channel,
        mcp_servers=file_config.mcp_servers,
        qq=_load_qq_config(env_path),
        provider=ProviderConfig(
            api_key=api_key,
            **file_config.provider.model_dump(),
        ),
    )


def get_env_value(name: str, env_path: str | Path = DEFAULT_ENV_PATH) -> str | None:
    """Read one value, preferring the process environment over ``.env``."""

    value = os.environ.get(name)
    if value is not None:
        return value

    try:
        lines = Path(env_path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, file_value = stripped.partition("=")
        if separator and key.strip() == name:
            return file_value.strip().strip("\"'")
    return None


def _load_qq_config(env_path: str | Path) -> QQChannelConfig | None:
    app_id = get_env_value(_QQ_APP_ID_ENV_VAR, env_path)
    secret = get_env_value(_QQ_SECRET_ENV_VAR, env_path)
    if not app_id and not secret:
        return None
    if not app_id or not secret:
        raise ConfigError("QQ configuration requires both app ID and secret")

    allow_from = get_env_value(_QQ_ALLOW_FROM_ENV_VAR, env_path) or "*"
    allowed_senders = [
        sender_id.strip()
        for sender_id in allow_from.split(",")
        if sender_id.strip()
    ]
    try:
        return QQChannelConfig(
            app_id=app_id,
            secret=secret,
            allow_from=allowed_senders or ["*"],
        )
    except ValidationError as exc:
        raise ConfigError("QQ configuration does not match the expected schema") from exc
