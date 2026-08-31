"""QQ text channel backed by the optional qq-botpy SDK."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ...bus import InboundMessage, MessageBus, OutboundMessage
from ...config import QQChannelConfig
from ..base import BaseChannel

logger = logging.getLogger(__name__)

_C2C = "c2c"
_GROUP = "group"


@dataclass(frozen=True)
class _QQChatContext:
    chat_type: str
    message_id: str


class QQChannel(BaseChannel):
    """Adapt QQ C2C and group @ messages to the internal bus."""

    def __init__(
        self,
        name: str,
        message_bus: MessageBus,
        config: QQChannelConfig,
        *,
        client_factory: Callable[[QQChannel], Any] | None = None,
    ) -> None:
        super().__init__(name, message_bus)
        if not isinstance(config, QQChannelConfig):
            raise TypeError("QQChannel requires a QQChannelConfig")

        self.config = config
        self._client_factory = client_factory or _create_botpy_client
        self._client: Any | None = None
        self._client_task: asyncio.Future[Any] | None = None
        self._chat_contexts: dict[tuple[str, str, str], _QQChatContext] = {}

    async def start(self) -> None:
        """Create the QQ client and begin listening for message events."""

        if self.started:
            return

        logger.info("Starting QQ channel")
        self._client = self._client_factory(self)
        start_result = self._client.start(self.config.app_id, self.config.secret)
        if inspect.isawaitable(start_result):
            self._client_task = asyncio.ensure_future(start_result)
        await super().start()
        logger.info("QQ channel started")

    async def stop(self) -> None:
        """Close the QQ client and wait for its listener task to finish."""

        client = self._client
        task = self._client_task
        self._client = None
        self._client_task = None
        logger.info("Stopping QQ channel")
        try:
            if client is not None:
                close_result = client.close()
                if inspect.isawaitable(close_result):
                    await close_result
        finally:
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await super().stop()
            logger.info("QQ channel stopped")

    async def handle_c2c_message(self, message: Any) -> InboundMessage | None:
        """Convert one qq-botpy C2C message event into an inbound message."""

        author = getattr(message, "author", None)
        sender_id = _required_text(
            getattr(author, "user_openid", None),
            "QQ C2C sender openid",
        )
        return await self._publish_inbound(message, _C2C, sender_id, sender_id)

    async def handle_group_at_message(self, message: Any) -> InboundMessage | None:
        """Convert one qq-botpy group @ bot event into an inbound message."""

        author = getattr(message, "author", None)
        sender_id = _required_text(
            getattr(author, "user_openid", None)
            or getattr(author, "member_openid", None),
            "QQ group sender openid",
        )
        chat_id = _required_text(
            getattr(message, "group_openid", None),
            "QQ group openid",
        )
        return await self._publish_inbound(message, _GROUP, sender_id, chat_id)

    async def send(self, message: OutboundMessage) -> None:
        """Send one Markdown response through the matching QQ API."""

        self._validate_outbound_message(message)
        if self._client is None:
            raise RuntimeError("QQChannel must be started before sending messages")

        context = self._chat_contexts.get(_context_key(message))
        chat_type = _metadata_text(message.metadata, "qq_chat_type") or (
            context.chat_type if context is not None else None
        )
        message_id = _metadata_text(message.metadata, "message_id") or (
            context.message_id if context is not None else None
        )
        if message_id is None:
            raise ValueError("QQ outbound message requires an originating message_id")

        api = self._client.api
        markdown = {"content": message.content}
        if chat_type == _C2C:
            result = api.post_c2c_message(
                openid=message.chat_id,
                msg_type=2,
                markdown=markdown,
                msg_id=message_id,
            )
        elif chat_type == _GROUP:
            result = api.post_group_message(
                group_openid=message.chat_id,
                msg_type=2,
                markdown=markdown,
                msg_id=message_id,
            )
        else:
            raise ValueError(f"Unknown QQ chat type for outbound message: {chat_type}")

        if inspect.isawaitable(result):
            await result

    async def _publish_inbound(
        self,
        event: Any,
        chat_type: str,
        sender_id: str,
        chat_id: str,
    ) -> InboundMessage | None:
        if not self.config.allows_sender(sender_id):
            logger.warning("Ignored QQ message from a sender outside the allow list")
            return None

        content = _required_text(getattr(event, "content", None), "QQ message content")
        message_id = _required_text(getattr(event, "id", None), "QQ message ID")
        session_id = f"{self.name}:{chat_id}"
        message = InboundMessage(
            channel=self.name,
            chat_id=chat_id,
            sender_id=sender_id,
            session_id=session_id,
            content=content,
            metadata={
                "message_id": message_id,
                "qq_chat_type": chat_type,
            },
        )
        self._chat_contexts[_context_key(message)] = _QQChatContext(
            chat_type=chat_type,
            message_id=message_id,
        )
        await self.message_bus.publish_inbound(message)
        return message


def _create_botpy_client(channel: QQChannel) -> Any:
    try:
        import botpy
    except ImportError as exc:
        raise RuntimeError(
            "qq-botpy is required to start QQChannel; install the qq-botpy package"
        ) from exc

    class BotPyQQClient(botpy.Client):
        async def on_c2c_message_create(self, message: Any) -> None:
            await channel.handle_c2c_message(message)

        async def on_group_at_message_create(self, message: Any) -> None:
            await channel.handle_group_at_message(message)

    # qq-botpy enables a rotating ``botpy.log`` file by default.
    return BotPyQQClient(
        botpy.Intents(public_messages=True),
        bot_log=False,
        ext_handlers=False,
    )


def _context_key(message: InboundMessage | OutboundMessage) -> tuple[str, str, str]:
    return message.chat_id, message.sender_id, message.session_id


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _metadata_text(metadata: Mapping[str, Any], name: str) -> str | None:
    value = metadata.get(name)
    return value if isinstance(value, str) and value.strip() else None
