"""Shared, explicit logging setup for the nanobot package."""

from __future__ import annotations

import logging
import os
from pathlib import Path

DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_LOG_LEVEL = "INFO"
LOG_LEVEL_ENV_VAR = "NANOBOT_LOG_LEVEL"
_HANDLER_MARKER = "_nanobot_default_handler"


def configure_logging(level: int | str = logging.INFO) -> logging.Logger:
    """Configure the ``nanobot`` logger with one consistent stderr handler.

    This function is safe to call repeatedly. It intentionally does not alter
    the application's root logger, so a host application remains responsible
    for configuring logging outside the ``nanobot`` namespace.
    """

    package_logger = logging.getLogger("nanobot")
    package_logger.setLevel(_resolve_level(level))
    package_logger.propagate = False

    handler = next(
        (
            current_handler
            for current_handler in package_logger.handlers
            if getattr(current_handler, _HANDLER_MARKER, False)
        ),
        None,
    )
    if handler is None:
        handler = logging.StreamHandler()
        setattr(handler, _HANDLER_MARKER, True)
        package_logger.addHandler(handler)

    handler.setLevel(logging.NOTSET)
    handler.setFormatter(logging.Formatter(DEFAULT_LOG_FORMAT, DEFAULT_DATE_FORMAT))
    return package_logger


def configure_logging_from_env(
    env_path: str | Path = ".env",
) -> logging.Logger:
    """Configure logging from ``NANOBOT_LOG_LEVEL`` in env or a local .env file.

    A process environment variable takes precedence over the local file. The
    fallback is ``INFO`` when neither source provides a value.
    """

    level = os.environ.get(LOG_LEVEL_ENV_VAR)
    if level is None:
        level = _read_env_value(Path(env_path), LOG_LEVEL_ENV_VAR)
    return configure_logging(level or DEFAULT_LOG_LEVEL)


def _resolve_level(level: int | str) -> int:
    if isinstance(level, bool):
        raise TypeError("Logging level must be an integer or level name")
    if isinstance(level, int):
        return level
    if isinstance(level, str):
        resolved = logging.getLevelNamesMapping().get(level.upper())
        if isinstance(resolved, int):
            return resolved
        raise ValueError(f"Unknown logging level: {level}")
    raise TypeError("Logging level must be an integer or level name")


def _read_env_value(path: Path, name: str) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if separator and key.strip() == name:
            return value.strip().strip("\"'")
    return None
