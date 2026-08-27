"""Focused tests for the shared nanobot logging setup."""

from __future__ import annotations

import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nanobot.logging import (
    DEFAULT_LOG_FORMAT,
    LOG_LEVEL_ENV_VAR,
    configure_logging,
    configure_logging_from_env,
)


class LoggingConfigurationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._package_logger = logging.getLogger("nanobot")
        self._original_level = self._package_logger.level
        self._original_propagate = self._package_logger.propagate
        self._original_handlers = list(self._package_logger.handlers)
        self._package_logger.handlers.clear()

    def tearDown(self) -> None:
        for handler in self._package_logger.handlers:
            handler.close()
        self._package_logger.handlers[:] = self._original_handlers
        self._package_logger.setLevel(self._original_level)
        self._package_logger.propagate = self._original_propagate

    def test_configures_one_package_handler_and_applies_the_requested_level(self) -> None:
        configured = configure_logging("DEBUG")
        configure_logging(logging.WARNING)

        self.assertIs(configured, self._package_logger)
        self.assertEqual(self._package_logger.level, logging.WARNING)
        self.assertFalse(self._package_logger.propagate)
        self.assertEqual(len(self._package_logger.handlers), 1)
        self.assertEqual(
            self._package_logger.handlers[0].formatter._style._fmt,
            DEFAULT_LOG_FORMAT,
        )

    def test_child_loggers_inherit_the_configured_level(self) -> None:
        child_logger = logging.getLogger("nanobot.tests.logging")
        original_level = child_logger.level
        child_logger.setLevel(logging.NOTSET)
        try:
            configure_logging("WARNING")
            self.assertFalse(child_logger.isEnabledFor(logging.INFO))
            self.assertTrue(child_logger.isEnabledFor(logging.WARNING))
        finally:
            child_logger.setLevel(original_level)

    def test_exception_logs_include_a_traceback(self) -> None:
        configure_logging("ERROR")
        test_logger = logging.getLogger("nanobot.tests.logging_trace")

        with self.assertLogs(test_logger, level="ERROR") as captured:
            try:
                raise RuntimeError("expected failure")
            except RuntimeError:
                test_logger.exception("Tool execution failed")

        output = "\n".join(captured.output)
        self.assertIn("Traceback", output)
        self.assertIn("RuntimeError: expected failure", output)

    def test_configures_from_dotenv_and_defaults_to_info(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(f"{LOG_LEVEL_ENV_VAR}=DEBUG\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                configured = configure_logging_from_env(env_path)
                self.assertEqual(configured.level, logging.DEBUG)

            missing_path = Path(directory) / "missing.env"
            with patch.dict(os.environ, {}, clear=True):
                configured = configure_logging_from_env(missing_path)
                self.assertEqual(configured.level, logging.INFO)

    def test_process_environment_overrides_dotenv_log_level(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(f"{LOG_LEVEL_ENV_VAR}=DEBUG\n", encoding="utf-8")
            with patch.dict(os.environ, {LOG_LEVEL_ENV_VAR: "ERROR"}, clear=True):
                configured = configure_logging_from_env(env_path)

        self.assertEqual(configured.level, logging.ERROR)
