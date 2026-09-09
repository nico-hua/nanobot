"""Focused tests for the local WebSocket Channel."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from aiohttp import ClientSession, ClientWebSocketResponse, WSMsgType

from nanobot.agent import AgentLoop, AgentRunner, ContextBuilder
from nanobot.bus import InboundMessage, MessageBus, OutboundMessage
from nanobot.channels import ChannelManager, WebSocketChannel
from nanobot.config import WebSocketChannelConfig
from nanobot.providers import BaseMessage, LLMProvider, LLMResponse
from nanobot.session import SessionManager
from nanobot.tools import Tool, ToolRegistry


class EchoProvider(LLMProvider):
    async def complete(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        del tools, max_tokens, temperature
        return LLMResponse(content=f"Reply: {messages[-1].content}")

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        del tools, max_tokens, temperature
        content = f"Reply: {messages[-1].content}"
        if on_delta is not None:
            await on_delta(content)
        return LLMResponse(content=content)


class WebSocketChannelTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._channels: list[WebSocketChannel] = []
        self._clients: list[ClientSession] = []
        self._sockets: list[ClientWebSocketResponse] = []

    async def asyncTearDown(self) -> None:
        for socket in reversed(self._sockets):
            if not socket.closed:
                await socket.close()
        for client in reversed(self._clients):
            await client.close()
        for channel in reversed(self._channels):
            await channel.stop()
        self._temporary_directory.cleanup()

    async def test_connection_receives_ready_event_without_agent_dependencies(self) -> None:
        bus = MessageBus()
        channel = await self._start_channel(bus)
        socket = await self._connect(channel)

        self.assertEqual(await socket.receive_json(timeout=1), {"type": "ready"})
        self.assertEqual(channel.connection_count, 1)
        self.assertIs(channel.message_bus, bus)
        self.assertFalse(hasattr(channel, "_agent_loop"))
        self.assertFalse(hasattr(channel, "_provider"))

    async def test_client_message_becomes_an_inbound_bus_message(self) -> None:
        bus = MessageBus()
        channel = await self._start_channel(bus)
        socket = await self._connect(channel)
        await socket.receive_json(timeout=1)

        await socket.send_json(
            {
                "type": "message",
                "chat_id": "chat-1",
                "session_id": "session-1",
                "content": "Hello",
            }
        )

        self.assertEqual(
            await asyncio.wait_for(bus.consume_inbound(), timeout=1),
            InboundMessage(
                channel="websocket",
                chat_id="chat-1",
                sender_id="websocket",
                session_id="session-1",
                content="Hello",
                metadata={"streaming": True},
            ),
        )

    async def test_missing_session_id_uses_the_channel_and_chat_id(self) -> None:
        bus = MessageBus()
        channel = await self._start_channel(bus)
        socket = await self._connect(channel)
        await socket.receive_json(timeout=1)

        await socket.send_json(
            {
                "type": "message",
                "chat_id": "chat-1",
                "content": "Hello",
            }
        )

        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=1)
        self.assertEqual(inbound.session_id, "websocket:chat-1")
        self.assertTrue(inbound.metadata["streaming"])

    async def test_outbound_messages_are_sent_only_to_the_matching_session(self) -> None:
        bus = MessageBus()
        channel = await self._start_channel(bus)
        first = await self._connect(channel)
        second = await self._connect(channel)
        await first.receive_json(timeout=1)
        await second.receive_json(timeout=1)
        await self._send_client_message(first, "chat-1", "session-1", "First")
        await self._send_client_message(second, "chat-2", "session-2", "Second")
        await bus.consume_inbound()
        await bus.consume_inbound()

        await channel.send(
            OutboundMessage(
                channel="websocket",
                chat_id="chat-1",
                sender_id="websocket",
                session_id="session-1",
                content="Reply one",
            )
        )

        self.assertEqual(
            await first.receive_json(timeout=1),
            {
                "type": "message",
                "chat_id": "chat-1",
                "session_id": "session-1",
                "content": "Reply one",
            },
        )
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(first.receive(), timeout=0.05)
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(second.receive(), timeout=0.05)

    async def test_one_browser_connection_can_switch_its_active_session(self) -> None:
        bus = MessageBus()
        channel = await self._start_channel(bus)
        socket = await self._connect(channel)
        await socket.receive_json(timeout=1)

        await self._send_client_message(socket, "chat-1", "session-1", "First")
        first_inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=1)
        self.assertEqual(first_inbound.session_id, "session-1")

        await self._send_client_message(socket, "chat-2", "session-2", "Second")
        second_inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=1)
        self.assertEqual(second_inbound.session_id, "session-2")

        await channel.send(
            OutboundMessage(
                channel="websocket",
                chat_id="chat-1",
                sender_id="websocket",
                session_id="session-1",
                content="Late first reply",
            )
        )
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(socket.receive(), timeout=0.05)

        await channel.send(
            OutboundMessage(
                channel="websocket",
                chat_id="chat-2",
                sender_id="websocket",
                session_id="session-2",
                content="Second reply",
            )
        )
        self.assertEqual(
            await socket.receive_json(timeout=1),
            {
                "type": "message",
                "chat_id": "chat-2",
                "session_id": "session-2",
                "content": "Second reply",
            },
        )

    async def test_delta_outbound_message_uses_a_delta_event_without_turn_end(self) -> None:
        bus = MessageBus()
        channel = await self._start_channel(bus)
        socket = await self._connect(channel)
        await socket.receive_json(timeout=1)
        await self._send_client_message(socket, "chat-1", "session-1", "Hello")
        await bus.consume_inbound()

        await channel.send(
            OutboundMessage(
                channel="websocket",
                chat_id="chat-1",
                sender_id="websocket",
                session_id="session-1",
                content="Partial reply",
                metadata={"event": "delta"},
            )
        )

        self.assertEqual(
            await socket.receive_json(timeout=1),
            {
                "type": "delta",
                "chat_id": "chat-1",
                "session_id": "session-1",
                "content": "Partial reply",
            },
        )
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(socket.receive(), timeout=0.05)

    async def test_tool_call_outbound_message_uses_a_tool_call_event(self) -> None:
        bus = MessageBus()
        channel = await self._start_channel(bus)
        socket = await self._connect(channel)
        await socket.receive_json(timeout=1)
        await self._send_client_message(socket, "chat-1", "session-1", "Hello")
        await bus.consume_inbound()

        await channel.send(
            OutboundMessage(
                channel="websocket",
                chat_id="chat-1",
                sender_id="websocket",
                session_id="session-1",
                content="",
                metadata={
                    "event": "tool_call",
                    "tool_call": {
                        "id": "call-1",
                        "name": "read_file",
                        "arguments": {"path": "README.md"},
                    },
                },
            )
        )

        self.assertEqual(
            await socket.receive_json(timeout=1),
            {
                "type": "tool_call",
                "chat_id": "chat-1",
                "session_id": "session-1",
                "tool_call": {
                    "id": "call-1",
                    "name": "read_file",
                    "arguments": {"path": "README.md"},
                },
            },
        )

    async def test_invalid_client_message_returns_an_error_event(self) -> None:
        channel = await self._start_channel(MessageBus())
        socket = await self._connect(channel)
        await socket.receive_json(timeout=1)

        await socket.send_str("not json")

        self.assertEqual(
            await socket.receive_json(timeout=1),
            {
                "type": "error",
                "code": "invalid_json",
                "message": "WebSocket message must be valid JSON",
            },
        )

    async def test_closed_connections_and_stop_release_resources(self) -> None:
        channel = await self._start_channel(MessageBus())
        socket = await self._connect(channel)
        await socket.receive_json(timeout=1)

        await socket.close()
        await _wait_until(lambda: channel.connection_count == 0)

        self.assertEqual(channel.connection_task_count, 0)

    async def test_client_disconnect_requests_stop_for_its_last_session(self) -> None:
        bus = MessageBus()
        channel = await self._start_channel(bus)
        socket = await self._connect(channel)
        await socket.receive_json(timeout=1)
        await self._send_client_message(socket, "chat-1", "session-1", "Hello")
        await bus.consume_inbound()

        await socket.close()
        stop = await asyncio.wait_for(bus.consume_inbound(), timeout=1)

        self.assertEqual(stop.content, "/stop")
        self.assertEqual(stop.chat_id, "chat-1")
        self.assertEqual(stop.session_id, "session-1")
        self.assertEqual(
            stop.metadata,
            {"streaming": True, "source": "websocket_disconnect"},
        )

    async def test_stop_closes_active_connections_and_releases_resources(self) -> None:
        channel = await self._start_channel(MessageBus())
        socket = await self._connect(channel)
        await socket.receive_json(timeout=1)
        await channel.stop()

        closed = await socket.receive(timeout=1)
        self.assertIn(
            closed.type,
            {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING},
        )
        self.assertFalse(channel.started)
        self.assertIsNone(channel.port)
        self.assertEqual(channel.connection_count, 0)
        self.assertEqual(channel.connection_task_count, 0)

    async def test_agent_output_flows_through_channel_manager_to_websocket(self) -> None:
        bus = MessageBus()
        channel = await self._start_channel(bus)
        manager = ChannelManager(bus, (channel,))
        loop = AgentLoop(
            AgentRunner(),
            EchoProvider(),
            ToolRegistry(),
            SessionManager(Path(self._temporary_directory.name) / "workspace"),
            ContextBuilder(self._temporary_directory.name),
            message_bus=bus,
        )
        await manager.start_all()
        worker = asyncio.create_task(loop.run())
        socket = await self._connect(channel)
        await socket.receive_json(timeout=1)

        try:
            await self._send_client_message(socket, "chat-1", "session-1", "Hello Agent")

            self.assertEqual(
                await socket.receive_json(timeout=1),
                {
                    "type": "delta",
                    "chat_id": "chat-1",
                    "session_id": "session-1",
                    "content": "Reply: Hello Agent",
                },
            )
            self.assertEqual(
                await socket.receive_json(timeout=1),
                {
                    "type": "turn_end",
                    "chat_id": "chat-1",
                    "session_id": "session-1",
                    "content": "Reply: Hello Agent",
                    "metadata": {
                        "streaming": True,
                        "event": "turn_end",
                        "tools_used": [],
                        "token_usage": None,
                        "stop_reason": None,
                    },
                },
            )
        finally:
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker
            await manager.stop_all()

    async def _start_channel(self, bus: MessageBus) -> WebSocketChannel:
        channel = WebSocketChannel(
            "websocket",
            bus,
            WebSocketChannelConfig(host="127.0.0.1", port=0),
        )
        await channel.start()
        self._channels.append(channel)
        self.assertIsNotNone(channel.port)
        return channel

    async def _connect(self, channel: WebSocketChannel):
        port = channel.port
        if port is None:
            raise AssertionError("WebSocket channel has no bound port")
        client = ClientSession()
        self._clients.append(client)
        socket = await client.ws_connect(f"http://127.0.0.1:{port}/ws")
        self._sockets.append(socket)
        return socket

    async def _send_client_message(
        self,
        socket,
        chat_id: str,
        session_id: str,
        content: str,
    ) -> None:
        await socket.send_json(
            {
                "type": "message",
                "chat_id": chat_id,
                "session_id": session_id,
                "content": content,
            }
        )


async def _wait_until(predicate: Callable[[], bool]) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=1)
