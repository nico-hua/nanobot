"""Shared, explicit logging setup for the nanobot package."""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import DEFAULT_CONFIG_PATH, load_file_config

DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_LOG_LEVEL = "INFO"
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


def configure_logging_from_config(
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> logging.Logger:
    """Configure logging from the non-sensitive JSON configuration file.

    The fallback is ``INFO`` when the configuration file has not yet been
    created. Invalid existing configuration remains an explicit error.
    """

    path = Path(config_path)
    if not path.is_file():
        return configure_logging(DEFAULT_LOG_LEVEL)
    return configure_logging(load_file_config(path).logging.level)


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
