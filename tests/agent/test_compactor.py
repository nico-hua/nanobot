"""Focused tests for LLM-backed session summary compaction."""

from __future__ import annotations

import unittest
from collections.abc import Awaitable, Callable, Sequence

from nanobot.agent import SessionCompactor, estimate_messages_tokens
from nanobot.providers import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
    ToolMessage,
)
from nanobot.session import Session
from nanobot.tools import Tool


class SummaryProvider(LLMProvider):
    def __init__(self, response: LLMResponse | Exception) -> None:
        self._response = response
        self.complete_calls: list[tuple[BaseMessage, ...]] = []

    async def complete(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        del tools, max_tokens, temperature
        self.complete_calls.append(tuple(messages))
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        raise AssertionError("SessionCompactor must not use streaming")


class SessionCompactorTest(unittest.IsolatedAsyncioTestCase):
    async def test_does_not_compact_when_raw_history_fits_the_threshold(self) -> None:
        old_turn, recent_turn = _turns()
        session = Session.create("session-1").with_messages((*old_turn, *recent_turn))
        provider = SummaryProvider(LLMResponse(content="unused"))
        compactor = SessionCompactor(
            provider,
            token_threshold=estimate_messages_tokens(session.messages) + 1,
            recent_token_budget=1,
        )

        compacted = await compactor.compact(session)

        self.assertIs(compacted, session)
        self.assertEqual(provider.complete_calls, [])

    async def test_generates_summary_without_removing_complete_tool_turns(self) -> None:
        old_turn, recent_turn = _turns()
        session = Session.create("session-1").with_messages((*old_turn, *recent_turn))
        provider = SummaryProvider(LLMResponse(content="The weather request was completed."))
        compactor = SessionCompactor(
            provider,
            token_threshold=estimate_messages_tokens(recent_turn) + 1,
            recent_token_budget=estimate_messages_tokens(recent_turn),
        )

        compacted = await compactor.compact(session)

        self.assertEqual(compacted.messages, session.messages)
        self.assertEqual(compacted.summary, "The weather request was completed.")
        self.assertEqual(compacted.summary_until, len(old_turn))
        self.assertEqual(compacted.messages[compacted.summary_until :], recent_turn)
        self.assertEqual(len(provider.complete_calls), 1)
        self.assertIn('"tool_call_id":"call-1"', provider.complete_calls[0][1].content)

    async def test_summary_failure_leaves_the_original_session_unchanged(self) -> None:
        old_turn, recent_turn = _turns()
        session = Session.create("session-1").with_messages((*old_turn, *recent_turn))
        provider = SummaryProvider(RuntimeError("provider unavailable"))
        compactor = SessionCompactor(
            provider,
            token_threshold=estimate_messages_tokens(recent_turn) + 1,
            recent_token_budget=estimate_messages_tokens(recent_turn),
        )

        compacted = await compactor.compact(session)

        self.assertIs(compacted, session)
        self.assertIsNone(session.summary)
        self.assertEqual(session.summary_until, 0)


def _turns() -> tuple[tuple[BaseMessage, ...], tuple[BaseMessage, ...]]:
    request = ToolCallRequest(
        id="call-1",
        name="get_weather",
        arguments={"city": "Shanghai"},
    )
    old_turn = (
        HumanMessage(content="What is the weather in Shanghai?"),
        AIMessage(content="I will check.", tool_calls=(request,)),
        ToolMessage(content="Sunny", tool_call_id="call-1"),
        AIMessage(content="Shanghai is sunny."),
    )
    recent_turn = (
        HumanMessage(content="What should I pack?"),
        AIMessage(content="Bring sunscreen."),
    )
    return old_turn, recent_turn
