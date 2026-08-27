"""Route-aware messages exchanged through the in-memory message bus."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InboundMessage:
    """One user message waiting for agent processing."""

    channel: str
    chat_id: str
    session_id: str
    content: str

    def __post_init__(self) -> None:
        _validate_routing(self.channel, self.chat_id, self.session_id)
        if not isinstance(self.content, str):
            raise TypeError("InboundMessage content must be a string")


@dataclass(frozen=True)
class OutboundMessage:
    """One final agent response ready for delivery to a channel."""

    channel: str
    chat_id: str
    session_id: str
    content: str

    def __post_init__(self) -> None:
        _validate_routing(self.channel, self.chat_id, self.session_id)
        if not isinstance(self.content, str):
            raise TypeError("OutboundMessage content must be a string")


def _validate_routing(channel: str, chat_id: str, session_id: str) -> None:
    for name, value in (
        ("channel", channel),
        ("chat_id", chat_id),
        ("session_id", session_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
