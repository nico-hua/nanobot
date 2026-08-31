"""Focused tests for configured external channel creation."""

from __future__ import annotations

import unittest
from pathlib import Path

from nanobot.bus import MessageBus, OutboundMessage
from nanobot.channels import (
    BaseChannel,
    ChannelFactory,
    QQChannel,
    create_default_channel_factory,
)
from nanobot.config import NanobotConfig, ProviderConfig, QQChannelConfig


class StubChannel(BaseChannel):
    async def send(self, message: OutboundMessage) -> None:
        self._validate_outbound_message(message)


class ChannelFactoryTest(unittest.TestCase):
    def test_registered_constructor_creates_selected_channel(self) -> None:
        bus = MessageBus()
        factory = ChannelFactory()
        factory.register("fake", lambda name, message_bus, config: StubChannel(name, message_bus))

        channel = factory.create("fake", bus, _config(default_channel="fake"))

        self.assertIsInstance(channel, StubChannel)
        self.assertEqual(channel.name, "fake")
        self.assertIs(channel.message_bus, bus)

    def test_default_factory_registers_qq_channel(self) -> None:
        bus = MessageBus()

        channel = create_default_channel_factory().create("qq", bus, _config())

        self.assertIsInstance(channel, QQChannel)
        self.assertEqual(channel.name, "qq")
        self.assertIs(channel.message_bus, bus)

    def test_unknown_channel_type_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported configured channel"):
            ChannelFactory().create("missing", MessageBus(), _config())

    def test_duplicate_channel_type_is_rejected(self) -> None:
        factory = ChannelFactory()
        factory.register("fake", lambda name, message_bus, config: StubChannel(name, message_bus))

        with self.assertRaisesRegex(ValueError, "already registered"):
            factory.register("fake", lambda name, message_bus, config: StubChannel(name, message_bus))

    def test_qq_channel_requires_qq_configuration(self) -> None:
        config = _config()
        config_without_qq = config.model_copy(update={"qq": None})

        with self.assertRaisesRegex(ValueError, "requires QQ credentials"):
            create_default_channel_factory().create("qq", MessageBus(), config_without_qq)


def _config(default_channel: str = "qq") -> NanobotConfig:
    return NanobotConfig(
        provider=ProviderConfig(
            type="openai_compat",
            api_key="test-key",
            api_base="https://example.test/v1",
            default_model="test-model",
        ),
        workspace=Path("workspace"),
        default_channel=default_channel,
        qq=QQChannelConfig(app_id="test-app-id", secret="test-secret"),
    )
