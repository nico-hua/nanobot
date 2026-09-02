"""Focused tests for LLM-backed long-term memory consolidation."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from nanobot.memory import (
    MemoryConsolidationOutcome,
    MemoryConsolidator,
    MemoryEventConsumer,
    MemoryStore,
)
from nanobot.providers import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
)
from nanobot.session import Session
from nanobot.tools import Tool


class ConsolidationProvider(LLMProvider):
    def __init__(self, responses: Sequence[LLMResponse | Exception]) -> None:
        self._responses = list(responses)
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
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        raise AssertionError("MemoryConsolidator must not use streaming")


class MemoryConsolidatorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._store = MemoryStore(Path(self._temporary_directory.name))

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_passes_existing_memory_and_completed_messages_then_updates_memory(self) -> None:
        self._store.write("The user prefers Chinese responses.")
        session = Session.create("memory-session").with_messages(_completed_messages())
        original_messages = session.messages
        provider = ConsolidationProvider(
            (LLMResponse(content="The user prefers concise Chinese responses."),)
        )

        updated = await MemoryConsolidator(provider, self._store).consolidate(
            session.messages
        )

        self.assertTrue(updated)
        self.assertEqual(
            self._store.read(),
            "The user prefers concise Chinese responses.",
        )
        self.assertEqual(session.messages, original_messages)
        self.assertEqual(len(provider.complete_calls), 1)
        request = json.loads(provider.complete_calls[0][1].content)
        self.assertEqual(
            request["existing_memory"], "The user prefers Chinese responses."
        )
        self.assertEqual(request["completed_messages"][0]["content"], "Remember my preference.")
        self.assertEqual(request["completed_messages"][1]["role"], "assistant")
        instructions = provider.complete_calls[0][0].content
        self.assertIn("## User Information", instructions)
        self.assertIn("## Preferences", instructions)
        self.assertIn("## Project Context", instructions)
        self.assertIn("## Important Notes", instructions)

    async def test_no_long_term_information_leaves_existing_memory_unchanged(self) -> None:
        self._store.write("Existing memory.")
        provider = ConsolidationProvider((LLMResponse(content=""),))

        updated = await MemoryConsolidator(provider, self._store).consolidate(
            _completed_messages()
        )

        self.assertFalse(updated)
        self.assertEqual(self._store.read(), "Existing memory.")

    async def test_provider_failure_leaves_existing_memory_unchanged(self) -> None:
        self._store.write("Existing memory.")
        provider = ConsolidationProvider((RuntimeError("provider unavailable"),))

        updated = await MemoryConsolidator(provider, self._store).consolidate(
            _completed_messages()
        )

        self.assertFalse(updated)
        self.assertEqual(self._store.read(), "Existing memory.")

    async def test_empty_or_invalid_results_do_not_overwrite_memory(self) -> None:
        invalid_tool_call = ToolCallRequest(
            id="call-1",
            name="write_memory",
            arguments={},
        )
        provider = ConsolidationProvider(
            (
                LLMResponse(content=" \n"),
                LLMResponse(content="Replacement", tool_calls=(invalid_tool_call,)),
            )
        )
        consolidator = MemoryConsolidator(provider, self._store)

        for _ in range(2):
            self._store.write("Existing memory.")
            updated = await consolidator.consolidate(_completed_messages())

            self.assertFalse(updated)
            self.assertEqual(self._store.read(), "Existing memory.")

    async def test_multiple_consolidations_replace_instead_of_appending_memory(self) -> None:
        provider = ConsolidationProvider(
            (
                LLMResponse(content="Stable project convention."),
                LLMResponse(content="Stable project convention."),
            )
        )
        consolidator = MemoryConsolidator(provider, self._store)

        self.assertTrue(await consolidator.consolidate(_completed_messages()))
        self.assertTrue(await consolidator.consolidate(_completed_messages()))

        self.assertEqual(self._store.read(), "Stable project convention.")
        self.assertEqual(self._store.read().count("Stable project convention."), 1)
        self.assertEqual(
            MemoryStore(self._temporary_directory.name).read(),
            "Stable project convention.",
        )

    async def test_reprocessing_one_event_replaces_instead_of_appending_memory(self) -> None:
        event = self._store.append_event("session-1", _completed_messages())
        provider = ConsolidationProvider(
            (
                LLMResponse(content="Stable project convention."),
                LLMResponse(content="Stable project convention."),
            )
        )
        consolidator = MemoryConsolidator(provider, self._store)

        self.assertEqual(
            await consolidator.consolidate_event(event),
            MemoryConsolidationOutcome.UPDATED,
        )
        self.assertEqual(
            await consolidator.consolidate_event(event),
            MemoryConsolidationOutcome.UPDATED,
        )

        self.assertEqual(self._store.read(), "Stable project convention.")
        self.assertEqual(self._store.read().count("Stable project convention."), 1)

    async def test_empty_successful_event_advances_the_memory_cursor(self) -> None:
        provider = ConsolidationProvider((LLMResponse(content=""),))
        consumer = MemoryEventConsumer(
            self._store,
            MemoryConsolidator(provider, self._store),
        )
        event = self._store.append_event("session-1", _completed_messages())

        consumer.wake()
        await consumer.wait()

        self.assertEqual(self._store.read_cursor(), event.event_id)
        self.assertEqual(self._store.read(), "")

    async def test_pending_events_are_consolidated_as_one_batch(self) -> None:
        provider = ConsolidationProvider((LLMResponse(content="Updated memory."),))
        consumer = MemoryEventConsumer(
            self._store,
            MemoryConsolidator(provider, self._store),
        )
        first_event = self._store.append_event(
            "session-1",
            (
                HumanMessage(content="First completed turn."),
                AIMessage(content="First answer."),
            ),
        )
        last_event = self._store.append_event(
            "session-2",
            (
                HumanMessage(content="Second completed turn."),
                AIMessage(content="Second answer."),
            ),
        )

        consumer.wake()
        await consumer.wait()

        self.assertEqual(len(provider.complete_calls), 1)
        request = json.loads(provider.complete_calls[0][1].content)
        self.assertEqual(
            [message["content"] for message in request["completed_messages"]],
            [
                "First completed turn.",
                "First answer.",
                "Second completed turn.",
                "Second answer.",
            ],
        )
        self.assertEqual(self._store.read_cursor(), last_event.event_id)
        self.assertEqual(first_event.event_id, 1)
        self.assertEqual(self._store.read(), "Updated memory.")

    async def test_failed_event_does_not_advance_the_memory_cursor(self) -> None:
        provider = ConsolidationProvider((RuntimeError("provider unavailable"),))
        consumer = MemoryEventConsumer(
            self._store,
            MemoryConsolidator(provider, self._store),
        )
        self._store.append_event("session-1", _completed_messages())

        consumer.wake()
        await consumer.wait()

        self.assertEqual(self._store.read_cursor(), 0)


def _completed_messages() -> tuple[BaseMessage, ...]:
    return (
        HumanMessage(content="Remember my preference."),
        AIMessage(content="I will keep responses concise."),
    )
