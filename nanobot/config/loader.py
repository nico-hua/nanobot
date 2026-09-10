"""Load local runtime configuration from ``nanobot.json``."""

from __future__ import annotations

import json
import secrets
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from pydantic import ValidationError

from .schema import (
    AuthConfig,
    NanobotConfig,
    NanobotFileConfig,
    ProviderConfig,
    QQChannelConfig,
    WebSocketChannelConfig,
)

DEFAULT_CONFIG_PATH = Path(".nanobot/nanobot.json")


class ConfigError(ValueError):
    """Raised when configuration cannot be loaded or persisted safely."""


def load_file_config(
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> NanobotFileConfig:
    """Load and validate the local JSON configuration file."""

    path = Path(config_path)
    return _validate_file_config(_read_config_document(path))


def load_nanobot_config(
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> NanobotConfig:
    """Load the complete runtime configuration from JSON only."""

    path = Path(config_path)
    file_config = load_file_config(path)
    file_config = _ensure_auth_token(file_config, path)

    workspace = file_config.workspace
    if not workspace.is_absolute():
        workspace = (path.parent / workspace).resolve()

    try:
        provider = ProviderConfig(**file_config.provider.model_dump())
    except ValidationError as exc:
        raise ConfigError("Configuration file does not match the expected schema") from exc

    selected_channel_config = file_config.channel.default_config()

    return NanobotConfig(
        workspace=workspace,
        context_window_tokens=file_config.agent.context_window_tokens,
        compaction_threshold_tokens=file_config.agent.compaction_threshold_tokens,
        compaction_recent_tokens=file_config.agent.compaction_recent_tokens,
        cron_timezone=file_config.cron.timezone,
        api=file_config.api,
        auth=file_config.auth,
        tools=file_config.tools,
        default_channel=file_config.channel.default,
        websocket=(
            selected_channel_config
            if isinstance(selected_channel_config, WebSocketChannelConfig)
            else None
        ),
        mcp_servers=file_config.mcp.servers,
        qq=(
            selected_channel_config
            if isinstance(selected_channel_config, QQChannelConfig)
            else None
        ),
        provider=provider,
    )


def _read_config_document(config_path: Path) -> dict[str, Any]:
    try:
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file was not found: {config_path}") from exc
    except OSError as exc:
        raise ConfigError(f"Configuration file could not be read: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Configuration file contains invalid JSON: {config_path}") from exc

    if not isinstance(raw_config, dict):
        raise ConfigError("Configuration file root must be a JSON object")
    return raw_config


def _validate_file_config(raw_config: dict[str, Any]) -> NanobotFileConfig:
    try:
        return NanobotFileConfig.model_validate(raw_config)
    except ValidationError as exc:
        raise ConfigError("Configuration file does not match the expected schema") from exc


def _ensure_auth_token(
    file_config: NanobotFileConfig,
    config_path: Path,
) -> NanobotFileConfig:
    """Generate and persist one token only when static authentication is enabled."""

    if not file_config.auth.enabled or file_config.auth.token:
        return file_config

    token = secrets.token_urlsafe(32)
    _persist_auth_token(config_path, token)
    return file_config.model_copy(
        update={"auth": AuthConfig(enabled=True, token=token)}
    )


def _persist_auth_token(config_path: Path, token: str) -> None:
    """Atomically update only the auth token while retaining other JSON settings."""

    raw_config = _read_config_document(config_path)
    raw_auth = raw_config.get("auth")
    if raw_auth is None:
        raw_auth = {}
    if not isinstance(raw_auth, dict):
        raise ConfigError("Authentication token could not be persisted")
    raw_auth["token"] = token
    raw_config["auth"] = raw_auth
    _write_config_document(config_path, raw_config, "Authentication token could not be persisted")


def _write_config_document(
    config_path: Path,
    raw_config: dict[str, Any],
    error_message: str,
) -> None:
    content = json.dumps(raw_config, ensure_ascii=False, indent=2) + "\n"
    _replace_file_contents(config_path, content, error_message)


def _replace_file_contents(path: Path, content: str, error_message: str) -> None:
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
        temporary_path.replace(path)
    except OSError as exc:
        raise ConfigError(error_message) from exc
    finally:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)
