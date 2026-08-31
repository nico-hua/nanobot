"""Focused tests for JSONL-backed session persistence."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nanobot.providers import AIMessage, HumanMessage, SystemMessage, ToolCallRequest, ToolMessage
from nanobot.session import SessionManager


class SessionManagerTest(unittest.TestCase):
    def test_get_or_create_returns_a_new_empty_session_with_a_safe_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            directory = workspace / "sessions"
            manager = SessionManager(workspace)

            session = manager.get_or_create("qq:group/../chat-1")
            saved = manager.save(session)

            self.assertEqual(saved.key, "qq:group/../chat-1")
            self.assertEqual(saved.messages, ())
            self.assertEqual(len(list(directory.glob("*.jsonl"))), 1)
            self.assertEqual(
                [path.name for path in directory.iterdir()],
                [path.name for path in directory.glob("*.jsonl")],
            )

    def test_saved_session_is_recovered_by_a_new_manager(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            first_manager = SessionManager(directory)
            session = first_manager.get_or_create("session-1").with_messages(
                (SystemMessage(content="You are helpful."), HumanMessage(content="Hello"))
            )
            saved = first_manager.save(session)

            restored = SessionManager(directory).get_or_create("session-1")

            self.assertEqual(restored, saved)

    def test_multiple_sessions_are_isolated_and_listed_stably(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = SessionManager(temporary_directory)
            manager.save(
                manager.get_or_create("beta").with_messages((HumanMessage(content="B"),))
            )
            manager.save(
                manager.get_or_create("alpha").with_messages((HumanMessage(content="A"),))
            )

            alpha = SessionManager(temporary_directory).get_or_create("alpha")
            beta = SessionManager(temporary_directory).get_or_create("beta")

            self.assertEqual(alpha.messages, (HumanMessage(content="A"),))
            self.assertEqual(beta.messages, (HumanMessage(content="B"),))
            self.assertEqual(
                [session.key for session in manager.list_sessions()],
                ["alpha", "beta"],
            )

    def test_delete_removes_a_saved_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = SessionManager(temporary_directory)
            manager.save(manager.get_or_create("session-1"))

            self.assertTrue(manager.delete("session-1"))
            self.assertFalse(manager.delete("session-1"))
            self.assertEqual(manager.list_sessions(), ())

    def test_tool_calls_and_tool_results_are_recovered_without_losing_fields(self) -> None:
        tool_call = ToolCallRequest(
            id="call-1",
            name="get_weather",
            arguments={"city": "Shanghai", "units": "celsius"},
        )
        messages = (
            HumanMessage(content="What is the weather?"),
            AIMessage(content="", tool_calls=(tool_call,)),
            ToolMessage(content='{"temperature": 25}', tool_call_id="call-1"),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = SessionManager(temporary_directory)
            saved = manager.save(manager.get_or_create("tool-session").with_messages(messages))

            restored = SessionManager(temporary_directory).get_or_create("tool-session")

            self.assertEqual(restored, saved)
            self.assertEqual(restored.messages[1].tool_calls, (tool_call,))
            self.assertEqual(restored.messages[2].tool_call_id, "call-1")

    def test_summary_and_its_message_boundary_are_recovered(self) -> None:
        messages = (
            HumanMessage(content="First question"),
            AIMessage(content="First answer"),
            HumanMessage(content="Second question"),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = SessionManager(temporary_directory)
            saved = manager.save(
                manager.get_or_create("summary-session")
                .with_messages(messages)
                .with_summary("The first question was answered.", 2)
            )

            restored = SessionManager(temporary_directory).get_or_create("summary-session")

            self.assertEqual(restored, saved)
            self.assertEqual(restored.summary, "The first question was answered.")
            self.assertEqual(restored.summary_until, 2)

    def test_failed_replace_keeps_the_existing_session_file_intact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = SessionManager(temporary_directory)
            original = manager.save(
                manager.get_or_create("session-1").with_messages(
                    (HumanMessage(content="Original"),)
                )
            )
            replacement = original.with_messages((HumanMessage(content="Replacement"),))

            with patch("nanobot.session.storage.os.replace", side_effect=OSError("disk error")):
                with self.assertRaisesRegex(OSError, "disk error"):
                    manager.save(replacement)

            restored = SessionManager(temporary_directory).get_or_create("session-1")
            self.assertEqual(restored, original)
            self.assertFalse(any(path.suffix == ".tmp" for path in Path(temporary_directory).iterdir()))
