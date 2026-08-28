"""Focused tests for the minimal nanobot process entry point."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Callable
from pathlib import Path

from nanobot.cli.main import main
from nanobot.config import MCPServerConfig, NanobotConfig, ProviderConfig


class FakeApplication:
    def __init__(self, *, failure: Exception | None = None, wait_for_stop: bool = False) -> None:
        self.failure = failure
        self.wait_for_stop = wait_for_stop
        self.run_calls = 0
        self.close_calls = 0
        self.stop_requests = 0
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        self.run_calls += 1
        if self.failure is not None:
            raise self.failure
        if self.wait_for_stop:
            await self._stop_event.wait()

    async def close(self) -> None:
        self.close_calls += 1

    def request_stop(self) -> None:
        self.stop_requests += 1
        self._stop_event.set()


class MainTest(unittest.TestCase):
    def test_loads_config_and_runs_application(self) -> None:
        config = _config()
        loaded_paths: list[Path] = []
        applications: list[FakeApplication] = []

        exit_code = main(
            ["--config", "custom.json"],
            config_loader=lambda path: _record_config_path(path, config, loaded_paths),
            application_factory=lambda received_config: _record_application(
                received_config,
                config,
                applications,
            ),
            signal_installer=_no_signal_handlers,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(loaded_paths, [Path("custom.json")])
        self.assertEqual(applications[0].run_calls, 1)
        self.assertEqual(applications[0].close_calls, 1)

    def test_workspace_override_is_passed_to_application(self) -> None:
        received_configs: list[NanobotConfig] = []

        exit_code = main(
            ["--workspace", "temporary-workspace"],
            config_loader=lambda path: _config(),
            application_factory=lambda config: _record_application_config(
                config,
                received_configs,
            ),
            signal_installer=_no_signal_handlers,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(received_configs[0].workspace, Path("temporary-workspace").resolve())

    def test_signal_handler_requests_a_graceful_stop(self) -> None:
        application = FakeApplication(wait_for_stop=True)
        uninstall_calls = 0

        def signal_installer(
            loop: asyncio.AbstractEventLoop,
            received_application: FakeApplication,
        ) -> Callable[[], None]:
            nonlocal uninstall_calls
            self.assertIs(received_application, application)
            loop.call_soon(received_application.request_stop)

            def uninstall() -> None:
                nonlocal uninstall_calls
                uninstall_calls += 1

            return uninstall

        exit_code = main(
            [],
            config_loader=lambda path: _config(),
            application_factory=lambda config: application,
            signal_installer=signal_installer,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(application.stop_requests, 1)
        self.assertEqual(application.close_calls, 1)
        self.assertEqual(uninstall_calls, 1)

    def test_failure_returns_non_zero_and_closes_application(self) -> None:
        application = FakeApplication(failure=RuntimeError("startup failed"))

        exit_code = main(
            [],
            config_loader=lambda path: _config(),
            application_factory=lambda config: application,
            signal_installer=_no_signal_handlers,
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(application.close_calls, 1)

    def test_config_load_failure_returns_non_zero(self) -> None:
        exit_code = main(
            [],
            config_loader=lambda path: _raise_config_error(path),
            signal_installer=_no_signal_handlers,
        )

        self.assertEqual(exit_code, 1)


def _config() -> NanobotConfig:
    return NanobotConfig(
        provider=ProviderConfig(
            type="openai_compat",
            api_key="test-key",
            api_base="https://example.test/v1",
            default_model="test-model",
        ),
        workspace=Path("workspace"),
        default_channel="fake",
        mcp_servers={"fake": MCPServerConfig(command="python")},
    )


def _record_config_path(
    path: str | Path,
    config: NanobotConfig,
    loaded_paths: list[Path],
) -> NanobotConfig:
    loaded_paths.append(Path(path))
    return config


def _record_application(
    received_config: NanobotConfig,
    expected_config: NanobotConfig,
    applications: list[FakeApplication],
) -> FakeApplication:
    if received_config is not expected_config:
        raise AssertionError("CLI did not pass the loaded config to Application")
    application = FakeApplication()
    applications.append(application)
    return application


def _record_application_config(
    config: NanobotConfig,
    received_configs: list[NanobotConfig],
) -> FakeApplication:
    received_configs.append(config)
    return FakeApplication()


def _no_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    application: FakeApplication,
) -> Callable[[], None]:
    del loop, application
    return lambda: None


def _raise_config_error(path: str | Path) -> NanobotConfig:
    del path
    raise RuntimeError("invalid configuration")
