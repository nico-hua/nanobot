"""Focused tests for workspace long-term memory reads."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nanobot.memory import MemoryStore
from nanobot.providers import AIMessage, HumanMessage, SystemMessage


class MemoryStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._workspace = Path(self._temporary_directory.name)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_reads_utf8_memory_file(self) -> None:
        memory_path = self._workspace / "memory" / "MEMORY.md"
        memory_path.parent.mkdir()
        memory_path.write_text("用户偏好使用中文。", encoding="utf-8")

        self.assertEqual(MemoryStore(self._workspace).read(), "用户偏好使用中文。")

    def test_returns_empty_content_when_memory_is_missing_or_empty(self) -> None:
        store = MemoryStore(self._workspace)

        self.assertEqual(store.read(), "")

        memory_path = self._workspace / "memory" / "MEMORY.md"
        memory_path.parent.mkdir()
        memory_path.write_text(" \n", encoding="utf-8")

        self.assertEqual(store.read(), "")

    def test_writes_memory_that_a_new_store_can_read(self) -> None:
        store = MemoryStore(self._workspace)

        store.write("A durable user preference.")

        self.assertEqual(
            MemoryStore(self._workspace).read(),
            "A durable user preference.",
        )

    def test_write_failure_keeps_existing_memory_file(self) -> None:
        store = MemoryStore(self._workspace)
        store.write("Existing memory.")

        with patch.object(Path, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                store.write("Replacement memory.")

        self.assertEqual(store.read(), "Existing memory.")

    def test_appends_events_and_reads_only_events_after_cursor(self) -> None:
        store = MemoryStore(self._workspace)
        first_event = store.append_event(
            "session-1",
            (HumanMessage(content="First question."), AIMessage(content="First answer.")),
        )
        recreated_store = MemoryStore(self._workspace)
        second_event = recreated_store.append_event(
            "session-2",
            (HumanMessage(content="Second question."), AIMessage(content="Second answer.")),
        )

        self.assertEqual(first_event.event_id, 1)
        self.assertEqual(second_event.event_id, 2)
        self.assertEqual(
            recreated_store.read_events_after(0),
            (first_event, second_event),
        )
        self.assertEqual(recreated_store.read_events_after(1), (second_event,))

    def test_cursor_is_atomic_and_persists_for_a_recreated_store(self) -> None:
        store = MemoryStore(self._workspace)
        store.update_cursor(4)

        self.assertEqual(MemoryStore(self._workspace).read_cursor(), 4)

        with patch.object(Path, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                store.update_cursor(5)

        self.assertEqual(MemoryStore(self._workspace).read_cursor(), 4)

    def test_rejects_system_messages_from_memory_events(self) -> None:
        store = MemoryStore(self._workspace)

        with self.assertRaisesRegex(ValueError, "system messages"):
            store.append_event("session-1", (SystemMessage(content="Internal prompt."),))
