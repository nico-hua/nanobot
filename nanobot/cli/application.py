"""Assembly and lifecycle management for one long-running Agent process."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import Any

from ..agent import AgentLoop, AgentRunner
from ..bus import MessageBus
from ..channels import BaseChannel, ChannelManager, QQChannel
from ..config import NanobotConfig, ProviderConfig, load_nanobot_config
from ..logging import configure_logging_from_config
from ..mcp import MCPProvider
from ..providers import AnthropicCompatProvider, LLMProvider, OpenAICompatProvider
from ..tools import ToolContext, ToolLoader, ToolRegistry

logger = logging.getLogger(__name__)

ProviderFactory = Callable[[ProviderConfig], LLMProvider]
ChannelFactory = Callable[[str, MessageBus, NanobotConfig], BaseChannel]
MCPProviderFactory = Callable[[ToolRegistry, Mapping[str, Any]], MCPProvider]
AgentLoopFactory = Callable[[AgentRunner, LLMProvider, ToolRegistry, MessageBus], AgentLoop]
ChannelManagerFactory = Callable[[MessageBus, tuple[BaseChannel, ...]], ChannelManager]


class Application:
    """Assemble and run the configured Agent components until stopped."""

    def __init__(
        self,
        config: NanobotConfig,
        *,
        provider_factory: ProviderFactory | None = None,
        channel_factory: ChannelFactory | None = None,
        mcp_provider_factory: MCPProviderFactory = MCPProvider,
        tool_loader: ToolLoader | None = None,
        agent_loop_factory: AgentLoopFactory | None = None,
        channel_manager_factory: ChannelManagerFactory = ChannelManager,
    ) -> None:
        if not isinstance(config, NanobotConfig):
            raise TypeError("Application requires a NanobotConfig")
        if config.workspace is None:
            raise ValueError("Application requires a configured workspace")

        configure_logging_from_config()
        self._config = config
        provider_factory = provider_factory or _create_provider
        channel_factory = channel_factory or _create_channel
        agent_loop_factory = agent_loop_factory or _create_agent_loop
        self._message_bus = MessageBus()
        self._tool_registry = ToolRegistry()
        self._tool_loader = tool_loader if tool_loader is not None else ToolLoader()
        self._tool_loader.load(
            self._tool_registry,
            ToolContext(workspace=config.workspace),
        )
        self._provider = provider_factory(config.provider)
        self._mcp_provider = mcp_provider_factory(
            self._tool_registry,
            config.mcp_servers,
        )
        self._agent_loop = agent_loop_factory(
            AgentRunner(),
            self._provider,
            self._tool_registry,
            self._message_bus,
        )
        channel = channel_factory(config.default_channel, self._message_bus, config)
        self._channel_manager = channel_manager_factory(self._message_bus, (channel,))
        self._agent_task: asyncio.Task[None] | None = None
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

    async def run(self) -> None:
        """Start components and wait until ``request_stop`` or cancellation."""

        await self.start()
        try:
            await self._stop_event.wait()
        except asyncio.CancelledError:
            await self.close()
            raise
        await self.close()

    async def start(self) -> None:
        """Connect MCP, then start the loop task and ChannelManager once."""

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
            finally:
                await self._cancel_agent_task()
                await self._mcp_provider.close()
            self._started = False
            logger.info("Application stopped")

    async def _cancel_agent_task(self) -> None:
        task = self._agent_task
        self._agent_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _create_provider(config: ProviderConfig) -> LLMProvider:
    if config.type == "openai_compat":
        return OpenAICompatProvider(
            api_key=config.api_key,
            api_base=config.api_base,
            default_model=config.default_model,
            default_max_tokens=config.default_max_tokens,
            default_temperature=config.default_temperature,
        )
    return AnthropicCompatProvider(
        api_key=config.api_key,
        api_base=config.api_base,
        default_model=config.default_model,
        default_max_tokens=config.default_max_tokens,
        default_temperature=config.default_temperature,
    )


def _create_channel(
    channel_name: str,
    message_bus: MessageBus,
    config: NanobotConfig,
) -> BaseChannel:
    if channel_name != "qq":
        raise ValueError(f"Unsupported configured channel: {channel_name}")
    if config.qq is None:
        raise ValueError("The qq channel requires QQ credentials in .env")
    return QQChannel(channel_name, message_bus, config.qq)


def _create_agent_loop(
    runner: AgentRunner,
    provider: LLMProvider,
    tool_registry: ToolRegistry,
    message_bus: MessageBus,
) -> AgentLoop:
    return AgentLoop(
        runner,
        provider,
        tool_registry,
        message_bus=message_bus,
    )
