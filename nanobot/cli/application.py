"""Assembly and lifecycle management for one long-running Agent process."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import Any

from ..agent import AgentLoop, AgentRunner
from ..bus import MessageBus
from ..channels import BaseChannel, ChannelManager, create_default_channel_factory
from ..config import NanobotConfig, ProviderConfig, load_nanobot_config
from ..mcp import MCPProvider
from ..providers import LLMProvider, create_default_provider_factory
from ..session import SessionManager
from ..tools import ToolContext, ToolLoader, ToolRegistry

logger = logging.getLogger(__name__)

ProviderCreator = Callable[[ProviderConfig], LLMProvider]
ChannelCreator = Callable[[str, MessageBus, NanobotConfig], BaseChannel]
MCPProviderFactory = Callable[[ToolRegistry, Mapping[str, Any]], MCPProvider]
AgentLoopFactory = Callable[
    [AgentRunner, LLMProvider, ToolRegistry, SessionManager, MessageBus],
    AgentLoop,
]
ChannelManagerFactory = Callable[[MessageBus, tuple[BaseChannel, ...]], ChannelManager]


class Application:
    """Assemble and run the configured Agent components until stopped."""

    def __init__(
        self,
        config: NanobotConfig,
        *,
        provider_factory: ProviderCreator | None = None,
        channel_factory: ChannelCreator | None = None,
        mcp_provider_factory: MCPProviderFactory = MCPProvider,
        tool_loader: ToolLoader | None = None,
        agent_loop_factory: AgentLoopFactory | None = None,
        channel_manager_factory: ChannelManagerFactory = ChannelManager,
    ) -> None:
        if not isinstance(config, NanobotConfig):
            raise TypeError("Application requires a NanobotConfig")
        if config.workspace is None:
            raise ValueError("Application requires a configured workspace")

        self._config = config
        provider_factory = provider_factory or create_default_provider_factory().create
        channel_factory = channel_factory or create_default_channel_factory().create
        agent_loop_factory = agent_loop_factory or _create_agent_loop
        self._message_bus = MessageBus()
        self._tool_registry = ToolRegistry()
        self._tool_loader = tool_loader if tool_loader is not None else ToolLoader()
        self._tool_loader.load(
            self._tool_registry,
            ToolContext(workspace=config.workspace),
        )
        self._provider = provider_factory(config.provider)
        self._session_manager = SessionManager(config.workspace)
        self._mcp_provider = mcp_provider_factory(
            self._tool_registry,
            config.mcp_servers,
        )
        self._agent_loop = agent_loop_factory(
            AgentRunner(),
            self._provider,
            self._tool_registry,
            self._session_manager,
            self._message_bus,
        )
        channel = channel_factory(config.default_channel, self._message_bus, config)
        self._channel_manager = channel_manager_factory(self._message_bus, (channel,))
        self._agent_task: asyncio.Task[None] | None = None
        self._channel_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._close_lock = asyncio.Lock()
        self._started = False
        self._closed = False

    @classmethod
    def from_default_config(cls) -> Application:
        """Create an application from the default JSON and environment paths."""

        return cls(load_nanobot_config())

    @property
    def message_bus(self) -> MessageBus:
        """Return the one bus shared by the Agent and selected Channel."""

        return self._message_bus

    @property
    def tool_registry(self) -> ToolRegistry:
        """Return the registry shared by built-in and MCP tools."""

        return self._tool_registry

    @property
    def mcp_provider(self) -> MCPProvider:
        """Return the MCP connection manager used by this application."""

        return self._mcp_provider

    @property
    def agent_loop(self) -> AgentLoop:
        """Return the AgentLoop running on this application's MessageBus."""

        return self._agent_loop

    @property
    def channel_manager(self) -> ChannelManager:
        """Return the ChannelManager for the selected configured Channel."""

        return self._channel_manager

    @property
    def agent_task(self) -> asyncio.Task[None] | None:
        """Return the AgentLoop task while it is managed by the application."""

        return self._agent_task

    @property
    def channel_task(self) -> asyncio.Task[None] | None:
        """Return the ChannelManager dispatcher task while it is supervised."""

        return self._channel_task

    async def run(self) -> None:
        """Run until stopped, cancelled, or a supervised task stops."""

        await self.start()
        stop_task = asyncio.create_task(self._stop_event.wait())
        try:
            done, _ = await asyncio.wait(
                (stop_task, *self._runtime_tasks()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_task not in done:
                self._raise_for_stopped_runtime_task(done)
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception:
            await self.close()
            raise
        else:
            await self.close()
        finally:
            if not stop_task.done():
                stop_task.cancel()
                try:
                    await stop_task
                except asyncio.CancelledError:
                    pass

    async def start(self) -> None:
        """Perform short startup and schedule the long-running tasks once."""

        if self._closed:
            raise RuntimeError("Application has already been closed")
        if self._started:
            return

        logger.info("Application starting")
        try:
            results = await self._mcp_provider.connect_all()
            failures = sum(not result.success for result in results)
            if failures:
                logger.warning("Some MCP servers failed to connect (count=%d)", failures)
            self._agent_task = asyncio.create_task(self._agent_loop.run())
            await self._channel_manager.start_all()
            self._channel_task = self._channel_manager.dispatcher_task
            if self._channel_task is None:
                raise RuntimeError("ChannelManager did not create a dispatcher task")
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception:
            logger.exception("Application failed to start")
            await self.close()
            raise

        self._started = True
        logger.info("Application started")

    def request_stop(self) -> None:
        """Request that ``run`` leaves its wait state and shuts down."""

        self._stop_event.set()

    async def close(self) -> None:
        """Release Channel, AgentLoop task, and MCP connections exactly once."""

        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._stop_event.set()
            logger.info("Application stopping")
            try:
                await self._channel_manager.stop_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Application failed while stopping the ChannelManager")
            finally:
                self._channel_task = None
                await self._cancel_agent_task()
                try:
                    await self._mcp_provider.close()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Application failed while closing MCP connections")
            self._started = False
            logger.info("Application stopped")

    async def _cancel_agent_task(self) -> None:
        task = self._agent_task
        self._agent_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def _runtime_tasks(self) -> tuple[asyncio.Task[None], asyncio.Task[None]]:
        if self._agent_task is None or self._channel_task is None:
            raise RuntimeError("Application runtime tasks have not been started")
        return self._agent_task, self._channel_task

    def _raise_for_stopped_runtime_task(
        self,
        completed_tasks: set[asyncio.Task[object]],
    ) -> None:
        for component_name, task in (
            ("AgentLoop", self._agent_task),
            ("ChannelManager", self._channel_task),
        ):
            if task is None or task not in completed_tasks:
                continue
            try:
                task.result()
            except asyncio.CancelledError:
                logger.info("Application background task was cancelled (component=%s)", component_name)
                raise
            except Exception:
                logger.exception("Application background task failed (component=%s)", component_name)
                raise
            logger.error("Application background task stopped unexpectedly (component=%s)", component_name)
            raise RuntimeError(f"{component_name} stopped unexpectedly")


def _create_agent_loop(
    runner: AgentRunner,
    provider: LLMProvider,
    tool_registry: ToolRegistry,
    session_manager: SessionManager,
    message_bus: MessageBus,
) -> AgentLoop:
    return AgentLoop(
        runner,
        provider,
        tool_registry,
        session_manager,
        message_bus=message_bus,
    )
