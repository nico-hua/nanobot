"""A small aiohttp adapter that delegates requests to ``AgentLoop``."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlparse

from aiohttp import web

from ..agent import AgentLoop
from ..bus import InboundMessage, OutboundMessage
from ..config import ApiConfig
from ..providers import BaseMessage
from ..session import Session, SessionManager

logger = logging.getLogger(__name__)

_API_CHANNEL = "api"
_API_SENDER = "api"
_MAX_REQUEST_BODY_BYTES = 1_048_576
_SHUTDOWN_TIMEOUT_SECONDS = 1.0


class HttpApiService:
    """Serve a minimal JSON-over-HTTP interface backed by ``AgentLoop``.

    The service owns HTTP routing and listener lifecycle only. It never
    accesses a Provider: each request becomes an ``InboundMessage`` handled by
    the existing AgentLoop path.
    """

    def __init__(
        self,
        agent_loop: AgentLoop,
        session_manager: SessionManager,
        config: ApiConfig,
    ) -> None:
        if not isinstance(config, ApiConfig):
            raise TypeError("HttpApiService requires an ApiConfig")
        if not isinstance(session_manager, SessionManager):
            raise TypeError("HttpApiService requires a SessionManager")

        self._agent_loop = agent_loop
        self._session_manager = session_manager
        self._config = config
        self._app = web.Application(
            client_max_size=_MAX_REQUEST_BODY_BYTES,
            middlewares=(_cors_middleware, _error_middleware),
        )
        self._app.router.add_get("/health", self._health)
        self._app.router.add_post("/v1/messages", self._post_message)
        self._app.router.add_get("/v1/sessions", self._list_sessions)
        self._app.router.add_get("/v1/sessions/{session_id}", self._get_session)
        self._runner: web.AppRunner | None = None
        self._started = False

    @property
    def started(self) -> bool:
        """Return whether the HTTP listener is accepting requests."""

        return self._started

    @property
    def port(self) -> int | None:
        """Return the bound port, including an OS-selected test port."""

        if self._runner is None:
            return None
        for address in self._runner.addresses:
            if isinstance(address, tuple) and len(address) >= 2:
                return int(address[1])
        return None

    async def start(self) -> None:
        """Bind the configured local listener once when the API is enabled."""

        if self._started or not self._config.enabled:
            return

        runner = web.AppRunner(
            self._app,
            access_log=None,
            shutdown_timeout=_SHUTDOWN_TIMEOUT_SECONDS,
        )
        try:
            await runner.setup()
            site = web.TCPSite(
                runner,
                host=self._config.host,
                port=self._config.port,
            )
            await site.start()
        except asyncio.CancelledError:
            await runner.cleanup()
            raise
        except Exception:
            await runner.cleanup()
            logger.exception("HTTP API service failed to start")
            raise

        self._runner = runner
        self._started = True
        logger.info(
            "HTTP API service started (host=%s, port=%s)",
            self._config.host,
            self.port,
        )

    async def stop(self) -> None:
        """Stop accepting requests and release aiohttp resources."""

        runner = self._runner
        self._runner = None
        if runner is not None:
            await runner.cleanup()

        if self._started:
            logger.info("HTTP API service stopped")
        self._started = False

    async def _health(self, request: web.Request) -> web.Response:
        del request
        return _json_response({"status": "ok"})

    async def _post_message(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except web.HTTPRequestEntityTooLarge:
            return _error_response(
                413,
                "payload_too_large",
                "Request body is too large",
            )
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _error_response(
                400,
                "invalid_json",
                "Request body must be valid JSON",
            )

        if not isinstance(payload, dict):
            return _error_response(
                400,
                "invalid_request",
                "Request JSON must be an object",
            )

        session_id = payload.get("session_id")
        content = payload.get("content")
        if not isinstance(session_id, str) or not session_id.strip():
            return _error_response(
                400,
                "invalid_session_id",
                "session_id must be a non-empty string",
            )
        if not isinstance(content, str):
            return _error_response(
                400,
                "invalid_content",
                "content must be a string",
            )

        inbound = InboundMessage(
            channel=_API_CHANNEL,
            chat_id=session_id.strip(),
            sender_id=_API_SENDER,
            session_id=session_id.strip(),
            content=content,
        )
        return await self._process_inbound(inbound)

    async def _list_sessions(self, request: web.Request) -> web.Response:
        """Return persisted sessions ordered by their most recent update."""

        del request
        sessions = sorted(
            self._session_manager.list_sessions(),
            key=lambda session: (session.updated_at, session.key),
            reverse=True,
        )
        return _json_response(
            {"sessions": [_session_summary(session) for session in sessions]}
        )

    async def _get_session(self, request: web.Request) -> web.Response:
        """Return the UI-visible history for one saved session."""

        session_id = request.match_info["session_id"]
        session = self._session_manager.get(session_id)
        if session is None:
            return _error_response(
                404,
                "session_not_found",
                "Session was not found",
            )
        return _json_response(
            {
                "session_id": session.key,
                "updated_at": session.updated_at.isoformat(),
                "messages": _visible_message_records(session.messages),
            }
        )

    async def _process_inbound(self, inbound: InboundMessage) -> web.Response:
        process_inbound = getattr(self._agent_loop, "process_inbound", None)
        if not callable(process_inbound):
            return _error_response(
                503,
                "agent_unavailable",
                "AgentLoop is unavailable",
            )

        try:
            response = await asyncio.wait_for(
                process_inbound(inbound),
                timeout=self._config.request_timeout_seconds,
            )
        except TimeoutError:
            return _error_response(
                504,
                "agent_timeout",
                "Agent response timed out",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("AgentLoop failed while handling an HTTP request")
            return _error_response(
                502,
                "agent_error",
                "Agent failed to process the message",
            )

        if response is None:
            # During an active goal, AgentLoop intentionally queues ordinary
            # user input for injection at the next tool boundary.
            return _json_response(
                {
                    "session_id": inbound.session_id,
                    "content": "Message queued for the active goal.",
                },
                status=202,
            )
        if not isinstance(response, OutboundMessage):
            return _error_response(
                502,
                "invalid_agent_response",
                "Agent returned an invalid response",
            )
        if response.channel != _API_CHANNEL or response.session_id != inbound.session_id:
            return _error_response(
                502,
                "invalid_agent_response",
                "Agent returned a mismatched response",
            )

        return _json_response(
            {
                "session_id": inbound.session_id,
                "content": response.content,
            }
        )


@web.middleware
async def _cors_middleware(
    request: web.Request,
    handler: Any,
) -> web.StreamResponse:
    """Allow the local Web UI to read the local-only API responses."""

    response = await handler(request)
    origin = request.headers.get("Origin")
    if _is_local_browser_origin(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    return response


@web.middleware
async def _error_middleware(
    request: web.Request,
    handler: Any,
) -> web.StreamResponse:
    """Convert framework-level request failures into the API JSON contract."""

    try:
        return await handler(request)
    except asyncio.CancelledError:
        raise
    except web.HTTPException as error:
        code, message = _framework_error_details(error.status)
        return _error_response(error.status, code, message)
    except Exception:
        logger.exception("HTTP API request failed")
        return _error_response(500, "internal_error", "Internal server error")


def _json_response(payload: dict[str, Any], *, status: int = 200) -> web.Response:
    return web.json_response(
        payload,
        status=status,
        dumps=lambda value: json.dumps(value, ensure_ascii=False),
    )


def _error_response(status: int, code: str, message: str) -> web.Response:
    return _json_response(
        {"error": {"code": code, "message": message}},
        status=status,
    )


def _framework_error_details(status: int) -> tuple[str, str]:
    return {
        404: ("not_found", "Route not found"),
        405: ("method_not_allowed", "Method not allowed"),
        413: ("payload_too_large", "Request body is too large"),
    }.get(status, ("request_error", "Request could not be processed"))


def _session_summary(session: Session) -> dict[str, Any]:
    messages = _visible_message_records(session.messages)
    return {
        "session_id": session.key,
        "updated_at": session.updated_at.isoformat(),
        "message_count": len(messages),
        "preview": _preview(messages),
    }


def _visible_message_records(
    messages: tuple[BaseMessage, ...],
) -> list[dict[str, str]]:
    """Keep the chat transcript separate from tool and system internals."""

    return [
        {"role": message.role, "content": message.content}
        for message in messages
        if message.role in {"user", "assistant"}
    ]


def _preview(messages: list[dict[str, str]], *, limit: int = 120) -> str:
    for message in reversed(messages):
        content = " ".join(message["content"].split())
        if content:
            return content if len(content) <= limit else f"{content[:limit - 1]}…"
    return ""


def _is_local_browser_origin(origin: str | None) -> bool:
    if not isinstance(origin, str):
        return False
    parsed = urlparse(origin)
    return parsed.scheme in {"http", "https"} and parsed.hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }
