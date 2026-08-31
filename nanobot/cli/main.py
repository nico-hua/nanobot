"""Process entry point for running the configured Application."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from collections.abc import Callable, Sequence
from pathlib import Path

from ..agent.logging import configure_logging, configure_logging_from_config
from ..config import DEFAULT_CONFIG_PATH, NanobotConfig, load_nanobot_config
from .application import Application

logger = logging.getLogger(__name__)

ConfigLoader = Callable[[str | Path], NanobotConfig]
ApplicationFactory = Callable[[NanobotConfig], Application]
SignalInstaller = Callable[[asyncio.AbstractEventLoop, Application], Callable[[], None]]


def main(
    argv: Sequence[str] | None = None,
    *,
    config_loader: ConfigLoader = load_nanobot_config,
    application_factory: ApplicationFactory = Application,
    signal_installer: SignalInstaller | None = None,
) -> int:
    """Load configuration and run the Application until it stops or fails."""

    configure_logging()
    arguments = _create_parser().parse_args(argv)
    config_path = Path(arguments.config)
    try:
        config = config_loader(config_path)
        if arguments.workspace is not None:
            config = config.model_copy(
                update={"workspace": Path(arguments.workspace).resolve()},
            )
        application = application_factory(config)
        configure_logging_from_config(config_path)
        asyncio.run(_run_application(application, signal_installer or _install_signal_handlers))
    except KeyboardInterrupt:
        logger.info("Application interrupted")
        return 0
    except Exception:
        logger.exception("Application failed to start or run")
        return 1
    return 0


async def _run_application(
    application: Application,
    signal_installer: SignalInstaller,
) -> None:
    """Run one Application while mapping process signals to a stop request."""

    uninstall = _noop
    try:
        uninstall = signal_installer(asyncio.get_running_loop(), application)
        await application.run()
    finally:
        try:
            uninstall()
        except Exception:
            logger.exception("Failed to remove application signal handlers")
        await application.close()


def _create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the nanobot Agent application.")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help="Path to the nanobot JSON configuration file.",
    )
    parser.add_argument(
        "--workspace",
        help="Override the workspace path for this run.",
    )
    return parser


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    application: Application,
) -> Callable[[], None]:
    """Install SIGINT/SIGTERM handlers with a Windows-compatible fallback."""

    removers: list[Callable[[], None]] = []
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(stop_signal, application.request_stop)
        except (NotImplementedError, RuntimeError):
            previous_handler = signal.getsignal(stop_signal)

            def request_stop(_signum: int, _frame: object) -> None:
                application.request_stop()

            signal.signal(stop_signal, request_stop)
            removers.append(
                lambda stop_signal=stop_signal, previous_handler=previous_handler: signal.signal(
                    stop_signal,
                    previous_handler,
                )
            )
        else:
            removers.append(lambda stop_signal=stop_signal: loop.remove_signal_handler(stop_signal))

    def uninstall() -> None:
        for remove in reversed(removers):
            remove()

    return uninstall


def _noop() -> None:
    """Provide a no-op signal cleanup callback before handlers are installed."""
