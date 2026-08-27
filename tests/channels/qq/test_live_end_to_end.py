"""Manual live test for the QQ -> Agent -> DeepSeek -> tool -> QQ flow.

This test is skipped by default because it opens a real QQ bot connection and
calls DeepSeek. To run it, set these environment variables before invoking
unittest:

* ``NANOBOT_RUN_QQ_DEEPSEEK_LIVE_TESTS=1``
* ``NANOBOT_QQ_APP_ID``
* ``NANOBOT_QQ_SECRET``
* ``NANOBOT_DEEPSEEK_API_KEY`` (or the existing ``DEEPSEEK_API_KEY``)

Optional variables:

* ``NANOBOT_QQ_ALLOW_FROM``: comma-separated QQ user openids; defaults to ``*``
* ``NANOBOT_DEEPSEEK_API_BASE``: defaults to ``https://api.deepseek.com``
* ``NANOBOT_DEEPSEEK_MODEL``: defaults to ``deepseek-chat``
* ``NANOBOT_QQ_LIVE_TEST_TIMEOUT_SECONDS``: defaults to ``180``

After the test starts, send the QQ bot this message through C2C or a group
@ mention: ``请使用 get_weather 工具查询北京天气，并简短回复。`` The test waits
for the reply, verifies that the Agent invoked the tool, and then closes all
background tasks and the QQ client.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from collections.abc import Sequence
from contextlib import suppress
from typing import Any

from nanobot.agent import AgentLoop, AgentRunner, SessionStore
from nanobot.bus import MessageBus, OutboundMessage
from nanobot.channels import ChannelManager
from nanobot.channels.qq import QQChannel
from nanobot.config import QQChannelConfig
from nanobot.providers import (
    BaseMessage,
    OpenAICompatProvider,
    SystemMessage,
    ToolMessage,
)
from nanobot.tools import Tool, ToolParameter, ToolRegistry, ToolResult

_RUN_LIVE_TESTS = os.getenv("NANOBOT_RUN_QQ_DEEPSEEK_LIVE_TESTS") == "1"
_QQ_APP_ID = os.getenv("NANOBOT_QQ_APP_ID")
_QQ_SECRET = os.getenv("NANOBOT_QQ_SECRET")
_QQ_ALLOW_FROM = os.getenv("NANOBOT_QQ_ALLOW_FROM", "*")
_DEEPSEEK_API_KEY = os.getenv("NANOBOT_DEEPSEEK_API_KEY") or os.getenv(
    "DEEPSEEK_API_KEY"
)
_DEEPSEEK_API_BASE = os.getenv(
    "NANOBOT_DEEPSEEK_API_BASE",
    os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com"),
)
_DEEPSEEK_MODEL = os.getenv(
    "NANOBOT_DEEPSEEK_MODEL",
    os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
)
_TIMEOUT_SECONDS = float(os.getenv("NANOBOT_QQ_LIVE_TEST_TIMEOUT_SECONDS", "180"))


class _PromptedSessionStore(SessionStore):
    """Add one deterministic tool-use instruction to new live-test sessions."""

    def load(self, session_id: str) -> tuple[BaseMessage, ...]:
        messages = super().load(session_id)
        if messages:
            return messages
        return (
            SystemMessage(
                content=(
                    "For weather requests, call get_weather exactly once before "
                    "answering. After receiving its result, answer concisely in Chinese."
                )
            ),
        )


class _WeatherTool(Tool):
    """A deterministic local tool used to prove the full tool-call round trip."""

    def __init__(self) -> None:
        super().__init__(
            name="get_weather",
            description="Return the current weather for a city.",
            parameters=(
                ToolParameter(
                    name="city",
                    description="The city to query.",
                    type="string",
                    required=True,
                ),
            ),
        )
        self.calls: list[dict[str, str]] = []

    async def execute(self, *, city: str) -> ToolResult:
        self.calls.append({"city": city})
        return ToolResult(content=f"{city}：晴，25°C。")


class _ReplyTrackingQQChannel(QQChannel):
    """Record successful QQ replies without changing the real sending path."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.sent_messages: list[OutboundMessage] = []
        self.reply_sent = asyncio.Event()

    async def send(self, message: OutboundMessage) -> None:
        await super().send(message)
        self.sent_messages.append(message)
        self.reply_sent.set()


@unittest.skipUnless(
    _RUN_LIVE_TESTS,
    "set NANOBOT_RUN_QQ_DEEPSEEK_LIVE_TESTS=1 to run this manual live test",
)
class QQDeepSeekEndToEndLiveTest(unittest.IsolatedAsyncioTestCase):
    """Exercise the real Channel, Bus, Loop, Runner, Provider, and tool chain."""

    async def asyncSetUp(self) -> None:
        missing = [
            name
            for name, value in (
                ("NANOBOT_QQ_APP_ID", _QQ_APP_ID),
                ("NANOBOT_QQ_SECRET", _QQ_SECRET),
                ("NANOBOT_DEEPSEEK_API_KEY or DEEPSEEK_API_KEY", _DEEPSEEK_API_KEY),
            )
            if not value
        ]
        if missing:
            self.skipTest(f"Missing required live-test environment variables: {', '.join(missing)}")
        if _TIMEOUT_SECONDS <= 0:
            self.skipTest("NANOBOT_QQ_LIVE_TEST_TIMEOUT_SECONDS must be positive")

        self._bus = MessageBus()
        self._sessions = _PromptedSessionStore()
        self._weather = _WeatherTool()
        self._channel = _ReplyTrackingQQChannel(
            "qq",
            self._bus,
            QQChannelConfig(
                app_id=_QQ_APP_ID or "",
                secret=_QQ_SECRET or "",
                allow_from=_parse_allow_from(_QQ_ALLOW_FROM),
            ),
        )
        self._manager = ChannelManager(self._bus, (self._channel,))
        self._loop = AgentLoop(
            runner=AgentRunner(),
            provider=OpenAICompatProvider(
                api_key=_DEEPSEEK_API_KEY or "",
                api_base=_DEEPSEEK_API_BASE,
                default_model=_DEEPSEEK_MODEL,
            ),
            tool_registry=ToolRegistry((self._weather,)),
            session_store=self._sessions,
            message_bus=self._bus,
        )
        self._loop_task: asyncio.Task[None] | None = None

        await self._manager.start_all()
        self._loop_task = asyncio.create_task(self._loop.run())

    async def asyncTearDown(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._loop_task
        await self._manager.stop_all()

    async def test_manual_qq_message_runs_agent_tool_loop_and_replies(self) -> None:
        """Wait for a QQ message, then assert the model completed a tool round trip."""

        await asyncio.wait_for(self._channel.reply_sent.wait(), timeout=_TIMEOUT_SECONDS)

        self.assertTrue(self._weather.calls, "The model did not invoke get_weather")
        self.assertTrue(self._channel.sent_messages, "No QQ reply was sent")
        outbound = self._channel.sent_messages[-1]
        self.assertEqual(outbound.channel, "qq")
        self.assertTrue(outbound.content.strip())

        history = self._sessions.load(outbound.session_id)
        self.assertTrue(
            any(isinstance(message, ToolMessage) for message in history),
            "The tool result was not retained in the Agent session history",
        )


def _parse_allow_from(value: str) -> Sequence[str]:
    senders = [sender.strip() for sender in value.split(",") if sender.strip()]
    return senders or ("*",)
