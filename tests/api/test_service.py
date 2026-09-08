"""Focused tests for the minimal HTTP AgentLoop adapter."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from aiohttp import ClientSession

from nanobot.agent import AgentLoop, AgentRunner, ContextBuilder
from nanobot.api import HttpApiService
from nanobot.bus import InboundMessage, MessageBus, OutboundMessage
from nanobot.config import ApiConfig
from nanobot.providers import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    LLMProvider,
    LLMResponse,
    SystemMessage,
    ToolMessage,
)
from nanobot.session import Session, SessionManager
from nanobot.tools import Tool, ToolRegistry


class EchoProvider(LLMProvider):
    def __init__(self) -> None:
        self.complete_calls: list[tuple[BaseMessage, ...]] = []

    async def complete(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        del tools, max_tokens, temperature
        request = tuple(messages)
        self.complete_calls.append(request)
        return LLMResponse(content=f"Reply: {request[-1].content}")

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        raise AssertionError("HTTP API tests do not use streaming")


class FailingProvider(EchoProvider):
    async def complete(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        del messages, tools, max_tokens, temperature
        raise RuntimeError("provider unavailable")


class BlockingProvider(EchoProvider):
    def __init__(self) -> None:
        super().__init__()
        self.first_call_started = asyncio.Event()
        self.release_first_call = asyncio.Event()

    async def complete(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        request = tuple(messages)
        self.complete_calls.append(request)
        del tools, max_tokens, temperature
        if len(self.complete_calls) == 1:
            self.first_call_started.set()
            await self.release_first_call.wait()
        return LLMResponse(content=f"Reply: {request[-1].content}")


class RecordingLoop:
    """A small AgentLoop double used to verify HTTP message conversion."""

    def __init__(self) -> None:
        self.received: list[InboundMessage] = []

    async def process_inbound(self, inbound: InboundMessage) -> OutboundMessage:
        self.received.append(inbound)
        return OutboundMessage(
            channel=inbound.channel,
            chat_id=inbound.chat_id,
            sender_id=inbound.sender_id,
            session_id=inbound.session_id,
            content="Recorded response",
        )


class HttpApiServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._services: list[HttpApiService] = []

    async def asyncTearDown(self) -> None:
        for service in reversed(self._services):
            await service.stop()
        self._temporary_directory.cleanup()

    async def test_post_converts_the_request_to_an_inbound_message(self) -> None:
        loop = RecordingLoop()
        service = await self._start_service(loop)

        status, payload = await _http_request(
            service,
            "POST",
            "/v1/messages",
            {"session_id": "session-1", "content": "Hello"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            payload,
            {"session_id": "session-1", "content": "Recorded response"},
        )
        self.assertEqual(
            loop.received,
            [
                InboundMessage(
                    channel="api",
                    chat_id="session-1",
                    sender_id="api",
                    session_id="session-1",
                    content="Hello",
                )
            ],
        )

    async def test_normal_request_uses_agent_loop_and_persists_its_session(self) -> None:
        provider = EchoProvider()
        sessions = SessionManager(Path(self._temporary_directory.name) / "workspace")
        loop = _agent_loop(provider, sessions, self._temporary_directory.name)
        service = await self._start_service(loop)

        status, payload = await _http_request(
            service,
            "POST",
            "/v1/messages",
            {"session_id": "stable-session", "content": "Hello Agent"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            payload,
            {"session_id": "stable-session", "content": "Reply: Hello Agent"},
        )
        self.assertEqual(provider.complete_calls[0][-1].content, "Hello Agent")
        saved_session = sessions.get_or_create("stable-session")
        self.assertEqual(saved_session.key, "stable-session")
        self.assertEqual(
            [message.content for message in saved_session.messages],
            ["Hello Agent", "Reply: Hello Agent"],
        )

    async def test_request_does_not_publish_the_external_message_to_message_bus(self) -> None:
        provider = EchoProvider()
        sessions = SessionManager(Path(self._temporary_directory.name) / "workspace")
        bus = MessageBus()
        service = await self._start_service(
            _agent_loop(
                provider,
                sessions,
                self._temporary_directory.name,
                message_bus=bus,
            )
        )

        status, _payload = await _http_request(
            service,
            "POST",
            "/v1/messages",
            {"session_id": "direct", "content": "Hello"},
        )

        self.assertEqual(status, 200)
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(bus.consume_inbound(), timeout=0.01)

    async def test_different_sessions_remain_isolated(self) -> None:
        provider = EchoProvider()
        sessions = SessionManager(Path(self._temporary_directory.name) / "workspace")
        service = await self._start_service(
            _agent_loop(provider, sessions, self._temporary_directory.name)
        )

        first, second = await asyncio.gather(
            _http_request(
                service,
                "POST",
                "/v1/messages",
                {"session_id": "first", "content": "One"},
            ),
            _http_request(
                service,
                "POST",
                "/v1/messages",
                {"session_id": "second", "content": "Two"},
            ),
        )

        self.assertEqual(first[1]["content"], "Reply: One")
        self.assertEqual(second[1]["content"], "Reply: Two")
        self.assertEqual(
            [message.content for message in sessions.get_or_create("first").messages],
            ["One", "Reply: One"],
        )
        self.assertEqual(
            [message.content for message in sessions.get_or_create("second").messages],
            ["Two", "Reply: Two"],
        )

    async def test_same_session_requests_are_serialized_by_agent_loop(self) -> None:
        provider = BlockingProvider()
        sessions = SessionManager(Path(self._temporary_directory.name) / "workspace")
        service = await self._start_service(
            _agent_loop(provider, sessions, self._temporary_directory.name)
        )

        first_request = asyncio.create_task(
            _http_request(
                service,
                "POST",
                "/v1/messages",
                {"session_id": "shared", "content": "First"},
            )
        )
        await asyncio.wait_for(provider.first_call_started.wait(), timeout=1)
        second_request = asyncio.create_task(
            _http_request(
                service,
                "POST",
                "/v1/messages",
                {"session_id": "shared", "content": "Second"},
            )
        )
        await asyncio.sleep(0)

        self.assertEqual(len(provider.complete_calls), 1)
        provider.release_first_call.set()
        first, second = await asyncio.gather(first_request, second_request)

        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 200)
        self.assertEqual(len(provider.complete_calls), 2)
        self.assertEqual(
            [message.content for message in sessions.get_or_create("shared").messages],
            ["First", "Reply: First", "Second", "Reply: Second"],
        )

    async def test_agent_failures_become_clear_http_errors(self) -> None:
        sessions = SessionManager(Path(self._temporary_directory.name) / "workspace")
        service = await self._start_service(
            _agent_loop(FailingProvider(), sessions, self._temporary_directory.name)
        )

        status, payload = await _http_request(
            service,
            "POST",
            "/v1/messages",
            {"session_id": "failure", "content": "Hello"},
        )

        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "agent_error")
        self.assertEqual(payload["error"]["message"], "Agent failed to process the message")

    async def test_invalid_request_returns_a_client_error(self) -> None:
        service = await self._start_service(RecordingLoop())

        status, payload = await _http_request(
            service,
            "POST",
            "/v1/messages",
            {"content": "Hello"},
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_session_id")

    async def test_health_endpoint_returns_ok(self) -> None:
        service = await self._start_service(RecordingLoop())

        status, payload = await _http_request(service, "GET", "/health")

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "ok"})

    async def test_session_list_and_history_expose_only_chat_messages(self) -> None:
        sessions = SessionManager(Path(self._temporary_directory.name) / "workspace")
        sessions.save(
            Session.create("session-one").with_messages(
                (
                    SystemMessage(content="Internal prompt"),
                    HumanMessage(content="First user message"),
                    AIMessage(content="First assistant reply"),
                    ToolMessage(content="Tool output", tool_call_id="call-1"),
                )
            )
        )
        sessions.save(
            Session.create("session-two").with_messages(
                (HumanMessage(content="Second session"),)
            )
        )
        service = await self._start_service(RecordingLoop(), sessions)

        status, payload = await _http_request(service, "GET", "/v1/sessions")

        self.assertEqual(status, 200)
        summaries = payload["sessions"]
        self.assertIsInstance(summaries, list)
        summary = next(item for item in summaries if item["session_id"] == "session-one")
        self.assertEqual(summary["message_count"], 2)
        self.assertEqual(summary["preview"], "First assistant reply")
        self.assertIn("updated_at", summary)

        status, history = await _http_request(
            service,
            "GET",
            "/v1/sessions/session-one",
        )

        self.assertEqual(status, 200)
        self.assertEqual(history["session_id"], "session-one")
        self.assertEqual(
            history["messages"],
            [
                {"role": "user", "content": "First user message"},
                {"role": "assistant", "content": "First assistant reply"},
            ],
        )

    async def test_missing_session_history_returns_a_clear_error(self) -> None:
        service = await self._start_service(RecordingLoop())

        status, payload = await _http_request(
            service,
            "GET",
            "/v1/sessions/missing-session",
        )

        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "session_not_found")

    async def test_local_browser_origin_can_read_session_data(self) -> None:
        service = await self._start_service(RecordingLoop())
        port = service.port
        if port is None:
            raise AssertionError("HTTP API service has no bound port")

        async with ClientSession() as client:
            async with client.get(
                f"http://127.0.0.1:{port}/v1/sessions",
                headers={"Origin": "http://localhost:5173"},
            ) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    response.headers["Access-Control-Allow-Origin"],
                    "http://localhost:5173",
                )

    async def test_router_returns_a_json_error_for_an_unsupported_method(self) -> None:
        service = await self._start_service(RecordingLoop())

        status, payload = await _http_request(service, "GET", "/v1/messages")

        self.assertEqual(status, 405)
        self.assertEqual(payload["error"]["code"], "method_not_allowed")

    async def _start_service(
        self,
        loop: AgentLoop | RecordingLoop,
        sessions: SessionManager | None = None,
    ) -> HttpApiService:
        service = HttpApiService(
            loop,
            sessions
            or SessionManager(Path(self._temporary_directory.name) / "api-workspace"),
            ApiConfig(
                enabled=True,
                host="127.0.0.1",
                port=0,
                request_timeout_seconds=1,
            ),
        )
        await service.start()
        self._services.append(service)
        self.assertIsNotNone(service.port)
        return service


def _agent_loop(
    provider: LLMProvider,
    sessions: SessionManager,
    workspace: str,
    *,
    message_bus: MessageBus | None = None,
) -> AgentLoop:
    return AgentLoop(
        AgentRunner(),
        provider,
        ToolRegistry(),
        sessions,
        ContextBuilder(workspace),
        message_bus=message_bus,
    )


async def _http_request(
    service: HttpApiService,
    method: str,
    path: str,
    payload: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    port = service.port
    if port is None:
        raise AssertionError("HTTP API service has no bound port")
    request_kwargs = {} if payload is None else {"json": payload}
    async with ClientSession() as client:
        async with client.request(
            method,
            f"http://127.0.0.1:{port}{path}",
            **request_kwargs,
        ) as response:
            return response.status, await response.json()
