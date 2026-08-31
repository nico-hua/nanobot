"""Focused tests for configured LLM provider creation."""

from __future__ import annotations

import unittest
from collections.abc import Awaitable, Callable, Sequence

from nanobot.config import ProviderConfig
from nanobot.providers import (
    AnthropicCompatProvider,
    BaseMessage,
    LLMProvider,
    LLMResponse,
    OpenAICompatProvider,
    ProviderFactory,
    create_default_provider_factory,
)
from nanobot.tools import Tool


class StubProvider(LLMProvider):
    async def complete(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        del messages, tools, max_tokens, temperature
        raise AssertionError("Provider factory tests do not make model calls")

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        del messages, tools, max_tokens, temperature, on_delta
        raise AssertionError("Provider factory tests do not make model calls")


class ProviderFactoryTest(unittest.TestCase):
    def test_registered_constructor_creates_selected_provider(self) -> None:
        provider = StubProvider()
        factory = ProviderFactory()
        factory.register("openai_compat", lambda config: provider)

        created = factory.create(_provider_config())

        self.assertIs(created, provider)

    def test_default_factory_registers_supported_provider_types(self) -> None:
        factory = create_default_provider_factory()

        openai_provider = factory.create(_provider_config(type="openai_compat"))
        anthropic_provider = factory.create(_provider_config(type="anthropic_compat"))

        self.assertIsInstance(openai_provider, OpenAICompatProvider)
        self.assertIsInstance(anthropic_provider, AnthropicCompatProvider)

    def test_unknown_provider_type_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported configured provider"):
            ProviderFactory().create(_provider_config())

    def test_duplicate_provider_type_is_rejected(self) -> None:
        factory = ProviderFactory()
        factory.register("openai_compat", lambda config: StubProvider())

        with self.assertRaisesRegex(ValueError, "already registered"):
            factory.register("openai_compat", lambda config: StubProvider())


def _provider_config(type: str = "openai_compat") -> ProviderConfig:
    return ProviderConfig(
        type=type,  # type: ignore[arg-type]
        api_key="test-key",
        api_base="https://example.test/v1",
        default_model="test-model",
        default_max_tokens=256,
        default_temperature=0.2,
    )
