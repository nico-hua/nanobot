"""Load non-sensitive JSON settings and merge secrets from ``.env``."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import ValidationError

from .schema import NanobotConfig, NanobotFileConfig, ProviderConfig

DEFAULT_CONFIG_PATH = Path(".nanobot/nanobot.json")
DEFAULT_ENV_PATH = Path(".env")
_API_KEY_ENV_VAR = "NANOBOT_API_KEY"


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
        default_channel=file_config.default_channel,
        mcp_servers=file_config.mcp_servers,
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
