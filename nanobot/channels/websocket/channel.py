"""A local WebSocket Channel that exchanges messages through ``MessageBus``."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any

from aiohttp import WSCloseCode, WSMsgType, web

from ...bus import MessageBus, OutboundMessage
from ...config import WebSocketChannelConfig
from ..base import BaseChannel

logger = logging.getLogger(__name__)

_WEBSOCKET_PATH = "/ws"
_WEBSOCKET_SENDER = "websocket"
_MAX_MESSAGE_BYTES = 1_048_576
_SHUTDOWN_TIMEOUT_SECONDS = 1.0


class WebSocketChannel(BaseChannel):
    """Route one browser-style WebSocket connection into the shared bus."""

    def __init__(
        self,
        name: str,
        message_bus: MessageBus,
        config: WebSocketChannelConfig,
    ) -> None:
        super().__init__(name, message_bus)
        if not isinstance(config, WebSocketChannelConfig):
            raise TypeError("WebSocketChannel requires a WebSocketChannelConfig")

        self.config = config
        self._app = web.Application(client_max_size=_MAX_MESSAGE_BYTES)
        self._app.router.add_get(_WEBSOCKET_PATH, self._handle_connection)
        self._runner: web.AppRunner | None = None
        self._connections_by_session: dict[str, web.WebSocketResponse] = {}
        self._connection_sessions: dict[web.WebSocketResponse, str | None] = {}
        self._connection_routes: dict[web.WebSocketResponse, tuple[str, str]] = {}
        self._connection_tasks: set[asyncio.Task[Any]] = set()
        self._stopping = False

    @property
    def port(self) -> int | None:
        """Return the bound port, including an OS-selected test port."""

        if self._runner is None:
            return None
        for address in self._runner.addresses:
            if isinstance(address, tuple) and len(address) >= 2:
                return int(address[1])
        return None

    @property
    def connection_count(self) -> int:
        """Return the number of active WebSocket connections."""

        return len(self._connection_sessions)

    @property
    def connection_task_count(self) -> int:
        """Return the number of connection handlers still running."""

        return sum(not task.done() for task in self._connection_tasks)

    async def start(self) -> None:
        """Bind the local WebSocket listener once."""

        if self.started:
            return

        self._stopping = False

        runner = web.AppRunner(
            self._app,
            access_log=None,
            shutdown_timeout=_SHUTDOWN_TIMEOUT_SECONDS,
        )
        try:
            await runner.setup()
            site = web.TCPSite(
                runner,
                host=self.config.host,
                port=self.config.port,
            )
            await site.start()
        except asyncio.CancelledError:
            await runner.cleanup()
            raise
        except Exception:
            await runner.cleanup()
            logger.exception("WebSocket channel failed to start")
            raise

        self._runner = runner
        await super().start()
        logger.info(
            "WebSocket channel started (host=%s, port=%s)",
            self.config.host,
            self.port,
        )

    async def stop(self) -> None:
        """Close active connections and release the aiohttp listener."""

        runner = self._runner
        self._runner = None
        self._stopping = True
        logger.info("Stopping WebSocket channel")
        try:
            await self._close_connections()
        finally:
            # The listener must still be released if cancelling the active
            # sockets itself is interrupted.
            try:
                if runner is not None:
                    await runner.cleanup()
            finally:
                try:
                    await self._cancel_connection_tasks()
                finally:
                    self._connections_by_session.clear()
                    self._connection_sessions.clear()
                    self._connection_routes.clear()
                    if self.started:
                        await super().stop()
                    self._stopping = False
                    logger.info("WebSocket channel stopped")

    async def send(self, message: OutboundMessage) -> None:
        """Send an Agent event to the connection for its session."""

        self._validate_outbound_message(message)
        if not self.started:
            raise RuntimeError("WebSocketChannel must be started before sending messages")

        connection = self._connections_by_session.get(message.session_id)
        if connection is None or connection.closed:
            if connection is not None:
                self._remove_connection(connection)
            logger.warning("Discarded outbound WebSocket message for a disconnected session")
            return

        try:
            event = message.metadata.get("event")
            if event == "delta":
                await self._send_event(
                    connection,
                    {
                        "type": "delta",
                        "chat_id": message.chat_id,
                        "session_id": message.session_id,
                        "content": message.content,
                    },
                )
                return
            if event == "tool_call":
                tool_call = message.metadata.get("tool_call")
                if (
                    not isinstance(tool_call, Mapping)
                    or not isinstance(tool_call.get("id"), str)
                    or not isinstance(tool_call.get("name"), str)
                    or not isinstance(tool_call.get("arguments"), Mapping)
                ):
                    raise ValueError("WebSocket tool_call events require call details")
                await self._send_event(
                    connection,
                    {
                        "type": "tool_call",
                        "chat_id": message.chat_id,
                        "session_id": message.session_id,
                        "tool_call": {
                            "id": tool_call["id"],
                            "name": tool_call["name"],
                            "arguments": dict(tool_call["arguments"]),
                        },
                    },
                )
                return
            if event == "turn_end":
                await self._send_event(
                    connection,
                    {
                        "type": "turn_end",
                        "chat_id": message.chat_id,
                        "session_id": message.session_id,
                        "content": message.content,
                        "metadata": dict(message.metadata),
                    },
                )
                return
            if event is None or event == "message":
                await self._send_event(
                    connection,
                    {
                        "type": "message",
                        "chat_id": message.chat_id,
                        "session_id": message.session_id,
                        "content": message.content,
                    },
                )
                return
            raise ValueError(f"Unsupported WebSocket outbound event: {event}")
        except asyncio.CancelledError:
            raise
        except Exception:
            self._remove_connection(connection)
            logger.warning("WebSocket connection failed while sending an outbound message")
            raise

    async def _handle_connection(self, request: web.Request) -> web.WebSocketResponse:
        connection = web.WebSocketResponse(max_msg_size=_MAX_MESSAGE_BYTES)
        await connection.prepare(request)
        task = asyncio.current_task()
        if task is not None:
            self._connection_tasks.add(task)
        self._connection_sessions[connection] = None

        try:
            await self._send_event(connection, {"type": "ready"})
            async for incoming in connection:
                if incoming.type is WSMsgType.TEXT:
                    await self._handle_client_text(connection, incoming.data)
                elif incoming.type is WSMsgType.BINARY:
                    await self._send_error(
                        connection,
                        "unsupported_message",
                        "WebSocket messages must contain JSON text",
                    )
                elif incoming.type is WSMsgType.ERROR:
                    logger.warning("WebSocket connection closed with an error")
                    break
        finally:
            try:
                if not self._stopping:
                    await self._request_stop_for_disconnected_session(connection)
            finally:
                self._remove_connection(connection)
                if task is not None:
                    self._connection_tasks.discard(task)
                if not connection.closed:
                    await connection.close()
        return connection

    async def _handle_client_text(
        self,
        connection: web.WebSocketResponse,
        text: str,
    ) -> None:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            await self._send_error(
                connection,
                "invalid_json",
                "WebSocket message must be valid JSON",
            )
            return

        if not isinstance(payload, dict):
            await self._send_error(
                connection,
                "invalid_message",
                "WebSocket message must be a JSON object",
            )
            return
        if payload.get("type") != "message":
            await self._send_error(
                connection,
                "unsupported_event",
                'WebSocket message type must be "message"',
            )
            return

        chat_id = payload.get("chat_id")
        content = payload.get("content")
        if not isinstance(chat_id, str) or not chat_id.strip():
            await self._send_error(
                connection,
                "invalid_chat_id",
                "chat_id must be a non-empty string",
            )
            return
        if not isinstance(content, str):
            await self._send_error(
                connection,
                "invalid_content",
                "content must be a string",
            )
            return

        session_id = self._resolve_session_id(payload.get("session_id"), chat_id)
        if session_id is None:
            await self._send_error(
                connection,
                "invalid_session_id",
                "session_id must be a string when provided",
            )
            return
        binding_error = self._bind_connection(connection, session_id)
        if binding_error is not None:
            await self._send_error(connection, binding_error[0], binding_error[1])
            return

        try:
            await self.receive_external(
                content,
                chat_id.strip(),
                _WEBSOCKET_SENDER,
                session_id,
                metadata={"streaming": self.config.streaming},
            )
            self._connection_routes[connection] = (chat_id.strip(), session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("WebSocket channel failed to publish an inbound message")
            await self._send_error(
                connection,
                "message_delivery_failed",
                "Message could not be accepted",
            )

    def _resolve_session_id(self, value: object, chat_id: str) -> str | None:
        if value is None:
            return f"{self.name}:{chat_id.strip()}"
        if not isinstance(value, str):
            return None
        return value.strip() or f"{self.name}:{chat_id.strip()}"

    def _bind_connection(
        self,
        connection: web.WebSocketResponse,
        session_id: str,
    ) -> tuple[str, str] | None:
        current_session = self._connection_sessions.get(connection)
        if current_session == session_id:
            return None

        existing_connection = self._connections_by_session.get(session_id)
        if (
            existing_connection is not None
            and existing_connection is not connection
            and not existing_connection.closed
        ):
            return (
                "session_in_use",
                "A connection is already active for this session",
            )

        # A browser connection can switch its active session.  Removing the
        # old routing entry prevents late output from appearing in the newly
        # selected conversation.
        if (
            current_session is not None
            and self._connections_by_session.get(current_session) is connection
        ):
            self._connections_by_session.pop(current_session, None)
        self._connection_sessions[connection] = session_id
        self._connections_by_session[session_id] = connection
        return None

    async def _close_connections(self) -> None:
        for connection in tuple(self._connection_sessions):
            if not connection.closed:
                await connection.close(code=WSCloseCode.GOING_AWAY)

    async def _cancel_connection_tasks(self) -> None:
        current_task = asyncio.current_task()
        tasks = tuple(
            task
            for task in self._connection_tasks
            if task is not current_task and not task.done()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _remove_connection(self, connection: web.WebSocketResponse) -> None:
        self._connection_routes.pop(connection, None)
        session_id = self._connection_sessions.pop(connection, None)
        if self._connections_by_session.get(session_id) is connection:
            self._connections_by_session.pop(session_id, None)

    async def _request_stop_for_disconnected_session(
        self,
        connection: web.WebSocketResponse,
    ) -> None:
        """Ask the normal command path to stop work owned by a closed browser."""

        route = self._connection_routes.get(connection)
        if route is None:
            return
        chat_id, session_id = route
        try:
            await self.receive_external(
                "/stop",
                chat_id,
                _WEBSOCKET_SENDER,
                session_id,
                metadata={
                    "streaming": self.config.streaming,
                    "source": "websocket_disconnect",
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("WebSocket channel failed to request turn cancellation")

    async def _send_error(
        self,
        connection: web.WebSocketResponse,
        code: str,
        message: str,
    ) -> None:
        await self._send_event(
            connection,
            {
                "type": "error",
                "code": code,
                "message": message,
            },
        )

    async def _send_event(
        self,
        connection: web.WebSocketResponse,
        event: dict[str, Any],
    ) -> None:
        await connection.send_json(
            event,
            dumps=lambda value: json.dumps(value, ensure_ascii=False),
        )
