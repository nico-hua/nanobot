"""Tests for QQChannel with a fake qq-botpy client."""

from __future__ import annotations

import asyncio
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from nanobot.bus import MessageBus, OutboundMessage
from nanobot.channels.qq import QQChannel
from nanobot.channels.qq.channel import _create_botpy_client
from nanobot.config import QQChannelConfig


class FakeQQAPI:
    def __init__(self) -> None:
        self.c2c_calls: list[dict[str, object]] = []
        self.group_calls: list[dict[str, object]] = []

    async def post_c2c_message(self, **arguments: object) -> None:
        self.c2c_calls.append(arguments)

    async def post_group_message(self, **arguments: object) -> None:
        self.group_calls.append(arguments)


class FakeQQClient:
    def __init__(self) -> None:
        self.api = FakeQQAPI()
        self.start_calls: list[tuple[str, str]] = []
        self.closed = False
        self._closed_event = asyncio.Event()

    async def start(self, app_id: str, secret: str) -> None:
        self.start_calls.append((app_id, secret))
        await self._closed_event.wait()

    async def close(self) -> None:
        self.closed = True
        self._closed_event.set()


class FakeBotPyIntents:
    def __init__(self, *, public_messages: bool) -> None:
        self.public_messages = public_messages


class FakeBotPySDKClient:
    instances: list[FakeBotPySDKClient] = []

    def __init__(self, *arguments, **keyword_arguments) -> None:
        self.arguments = arguments
        self.keyword_arguments = keyword_arguments
        self.instances.append(self)


def c2c_event(
    *,
    content: str = "Hello",
    message_id: str = "c2c-message-1",
    user_openid: str = "user-1",
) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        id=message_id,
        author=SimpleNamespace(user_openid=user_openid),
    )


def group_event(
    *,
    content: str = "Hello group",
    message_id: str = "group-message-1",
    member_openid: str = "member-1",
    group_openid: str = "group-1",
) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        id=message_id,
        author=SimpleNamespace(member_openid=member_openid),
        group_openid=group_openid,
    )


class QQChannelTest(unittest.IsolatedAsyncioTestCase):
    async def test_converts_c2c_message_to_inbound_message(self) -> None:
        bus = MessageBus()
        channel = _channel(bus)

        inbound = await channel.handle_c2c_message(c2c_event())

        self.assertIsNotNone(inbound)
        self.assertEqual(await bus.consume_inbound(), inbound)
        self.assertEqual(inbound.sender_id, "user-1")
        self.assertEqual(inbound.chat_id, "user-1")
        self.assertEqual(inbound.session_id, "qq:user-1")
        self.assertEqual(inbound.metadata["message_id"], "c2c-message-1")
        self.assertEqual(inbound.metadata["qq_chat_type"], "c2c")

    async def test_converts_group_at_message_to_inbound_message(self) -> None:
        bus = MessageBus()
        channel = _channel(bus)

        inbound = await channel.handle_group_at_message(group_event())

        self.assertIsNotNone(inbound)
        self.assertEqual(await bus.consume_inbound(), inbound)
        self.assertEqual(inbound.sender_id, "member-1")
        self.assertEqual(inbound.chat_id, "group-1")
        self.assertEqual(inbound.session_id, "qq:group-1")
        self.assertEqual(inbound.metadata["message_id"], "group-message-1")
        self.assertEqual(inbound.metadata["qq_chat_type"], "group")

    async def test_sends_markdown_with_the_matching_qq_api(self) -> None:
        bus = MessageBus()
        client = FakeQQClient()
        channel = _channel(bus, client)
        await channel.start()
        await asyncio.sleep(0)
        c2c_inbound = await channel.handle_c2c_message(c2c_event())
        group_inbound = await channel.handle_group_at_message(group_event())

        await channel.send(_outbound(c2c_inbound, "C2C reply"))
        await channel.send(_outbound(group_inbound, "Group reply"))
        await channel.stop()

        self.assertEqual(
            client.api.c2c_calls,
            [
                {
                    "openid": "user-1",
                    "msg_type": 2,
                    "markdown": {"content": "C2C reply"},
                    "msg_id": "c2c-message-1",
                }
            ],
        )
        self.assertEqual(
            client.api.group_calls,
            [
                {
                    "group_openid": "group-1",
                    "msg_type": 2,
                    "markdown": {"content": "Group reply"},
                    "msg_id": "group-message-1",
                }
            ],
        )

    async def test_sends_an_initiated_message_without_an_originating_message_id(self) -> None:
        bus = MessageBus()
        client = FakeQQClient()
        channel = _channel(bus, client)
        await channel.start()
        await asyncio.sleep(0)

        # Populate the chat cache first.  The initiated marker must still
        # prevent a scheduled message from replying with this message ID.
        await channel.handle_c2c_message(c2c_event())
        await channel.handle_group_at_message(group_event())

        await channel.send(
            OutboundMessage(
                channel="qq",
                chat_id="user-1",
                sender_id="cron",
                session_id="qq:user-1",
                content="Scheduled greeting",
                metadata={
                    "qq_chat_type": "c2c",
                    "source": "cron",
                },
            )
        )
        await channel.send(
            OutboundMessage(
                channel="qq",
                chat_id="group-1",
                sender_id="cron",
                session_id="qq:group-1",
                content="Scheduled group greeting",
                metadata={
                    "qq_chat_type": "group",
                    "source": "cron",
                },
            )
        )
        await channel.stop()

        self.assertEqual(
            client.api.c2c_calls,
            [
                {
                    "openid": "user-1",
                    "msg_type": 2,
                    "markdown": {"content": "Scheduled greeting"},
                }
            ],
        )
        self.assertEqual(
            client.api.group_calls,
            [
                {
                    "group_openid": "group-1",
                    "msg_type": 2,
                    "markdown": {"content": "Scheduled group greeting"},
                }
            ],
        )

    async def test_allow_from_rejects_unauthorized_sender(self) -> None:
        bus = MessageBus()
        channel = _channel(
            bus,
            config=QQChannelConfig(
                app_id="app",
                secret="secret",
                allow_from=["allowed-user"],
            ),
        )

        inbound = await channel.handle_c2c_message(c2c_event(user_openid="blocked-user"))

        self.assertIsNone(inbound)

    async def test_stop_releases_the_qq_client(self) -> None:
        client = FakeQQClient()
        channel = _channel(MessageBus(), client)

        await channel.start()
        await asyncio.sleep(0)
        await channel.stop()

        self.assertEqual(client.start_calls, [("app", "secret")])
        self.assertTrue(client.closed)
        self.assertFalse(channel.started)

    async def test_missing_qq_sdk_reports_a_clear_start_error(self) -> None:
        bus = MessageBus()
        with patch(
            "nanobot.channels.qq.channel._create_botpy_client",
            side_effect=RuntimeError("qq-botpy is required to start QQChannel"),
        ):
            channel = QQChannel("qq", bus, QQChannelConfig(app_id="app", secret="secret"))
            with self.assertRaisesRegex(RuntimeError, "qq-botpy is required"):
                await channel.start()

    async def test_sdk_client_disables_botpy_file_logging(self) -> None:
        FakeBotPySDKClient.instances.clear()
        fake_botpy = SimpleNamespace(
            Client=FakeBotPySDKClient,
            Intents=FakeBotPyIntents,
        )
        with patch.dict(sys.modules, {"botpy": fake_botpy}):
            client = _create_botpy_client(_channel(MessageBus()))

        self.assertIsInstance(client.arguments[0], FakeBotPyIntents)
        self.assertTrue(client.arguments[0].public_messages)
        self.assertEqual(
            client.keyword_arguments,
            {"bot_log": False, "ext_handlers": False},
        )



def _channel(
    bus: MessageBus,
    client: FakeQQClient | None = None,
    config: QQChannelConfig | None = None,
) -> QQChannel:
    return QQChannel(
        "qq",
        bus,
        config or QQChannelConfig(app_id="app", secret="secret"),
        client_factory=(lambda _: client) if client is not None else lambda _: FakeQQClient(),
    )


def _outbound(inbound, content: str) -> OutboundMessage:
    return OutboundMessage(
        channel=inbound.channel,
        chat_id=inbound.chat_id,
        sender_id=inbound.sender_id,
        session_id=inbound.session_id,
        content=content,
    )
