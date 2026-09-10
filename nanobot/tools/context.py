"""Shared dependencies used while creating tools."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..bus import MessageBus
    from ..cron import CronService
    from ..session import SessionManager
    from ..subagent import SubagentManager


_CURRENT_REQUEST_CONTEXT: ContextVar[RequestContext | None] = ContextVar(
    "nanobot_tool_request_context",
    default=None,
)


@dataclass(frozen=True)
class ToolContext:
    """Dependencies available to tool factories.

    Workspace is optional so tools can decide whether they are enabled for a
    particular agent configuration. Runtime-owned services are injected here
    while the application assembles built-in tools.
    """

    workspace: str | Path | None = None
    cron_service: CronService | None = None
    cron_timezone: str = "Asia/Shanghai"
    web_search_tavily_api_key: str = field(default="", repr=False)
    subagent_manager: SubagentManager | None = None
    session_manager: SessionManager | None = None
    message_bus: MessageBus | None = None


@dataclass(frozen=True)
class RequestContext:
    """Route information available to tools during one AgentRunner execution."""

    session_key: str
    channel: str
    chat_id: str
    sender_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    is_goal_mode: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("session_key", self.session_key),
            ("channel", self.channel),
            ("chat_id", self.chat_id),
            ("sender_id", self.sender_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Request context {name} must be a non-empty string")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("Request context metadata must be a mapping")
        if not isinstance(self.is_goal_mode, bool):
            raise TypeError("Request context is_goal_mode must be a bool")
        object.__setattr__(self, "metadata", dict(self.metadata))


def get_request_context() -> RequestContext | None:
    """Return the route information bound to the current AgentRunner task."""

    return _CURRENT_REQUEST_CONTEXT.get()


@contextmanager
def bind_request_context(context: RequestContext) -> Iterator[None]:
    """Bind request route information for one synchronous or asynchronous scope."""

    if not isinstance(context, RequestContext):
        raise TypeError("Request context binding requires a RequestContext")
    token = _CURRENT_REQUEST_CONTEXT.set(context)
    try:
        yield
    finally:
        _CURRENT_REQUEST_CONTEXT.reset(token)
