"""Tests for workspace-based system prompt construction."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nanobot.agent import (
    ContextBuilder,
    estimate_message_tokens,
    estimate_messages_tokens,
)
from nanobot.providers import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolCallRequest,
    ToolMessage,
)


class ContextBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._workspace = Path(self._temporary_directory.name)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_builds_prompt_with_all_workspace_context_files(self) -> None:
        self._write("AGENTS.md", "Follow the workspace rules.")
        self._write("SOUL.md", "Be concise and curious.")
        self._write("USER.md", "The user prefers Chinese.")

        prompt = ContextBuilder(self._workspace).build_system_prompt()

        self.assertIn("You are Nanobot, a helpful AI assistant.", prompt)
        self.assertIn(f"`{self._workspace.resolve()}`", prompt)
        self.assertIn("## Workspace Instructions\n\nFollow the workspace rules.", prompt)
        self.assertIn("## Agent Style\n\nBe concise and curious.", prompt)
        self.assertIn("## User Profile\n\nThe user prefers Chinese.", prompt)

    def test_skips_missing_and_empty_context_files(self) -> None:
        self._write("AGENTS.md", "")
        self._write("SOUL.md", "   \n")

        prompt = ContextBuilder(self._workspace).build_system_prompt()

        self.assertIn("# Nanobot", prompt)
        self.assertNotIn("## Workspace Instructions", prompt)
        self.assertNotIn("## Agent Style", prompt)
        self.assertNotIn("## User Profile", prompt)

    def test_uses_a_stable_context_file_order(self) -> None:
        self._write("AGENTS.md", "agents")
        self._write("SOUL.md", "soul")
        self._write("USER.md", "user")

        prompt = ContextBuilder(self._workspace).build_system_prompt()

        self.assertLess(
            prompt.index("## Workspace Instructions"),
            prompt.index("## Agent Style"),
        )
        self.assertLess(
            prompt.index("## Agent Style"),
            prompt.index("## User Profile"),
        )

    def test_reloads_files_without_modifying_the_workspace(self) -> None:
        path = self._write("SOUL.md", "First style.")
        before = path.read_bytes(), path.stat().st_mtime_ns
        builder = ContextBuilder(self._workspace)

        first_prompt = builder.build_system_prompt()
        after_first_build = path.read_bytes(), path.stat().st_mtime_ns
        path.write_text("Second style.", encoding="utf-8")
        second_prompt = builder.build_system_prompt()

        self.assertIn("First style.", first_prompt)
        self.assertIn("Second style.", second_prompt)
        self.assertNotIn("First style.", second_prompt)
        self.assertEqual(after_first_build, before)
        self.assertFalse((self._workspace / "sessions").exists())

    def test_keeps_all_history_when_it_fits_the_token_budget(self) -> None:
        history = (
            HumanMessage(content="First question."),
            AIMessage(content="First answer."),
            HumanMessage(content="Second question."),
            AIMessage(content="Second answer."),
        )
        builder = ContextBuilder(
            self._workspace,
            history_token_budget=estimate_messages_tokens(history),
        )

        messages = builder.build_request_messages(
            history,
            HumanMessage(content="Current question."),
        )

        self.assertEqual(messages[1:-1], history)
        self.assertEqual(messages[-1], HumanMessage(content="Current question."))

    def test_trims_to_complete_recent_turns_without_orphaning_tool_results(self) -> None:
        tool_call = ToolCallRequest(
            id="call-1",
            name="get_weather",
            arguments={"city": "Shanghai"},
        )
        older_turn = (
            HumanMessage(content="Old question " * 20),
            AIMessage(content="Calling a tool.", tool_calls=(tool_call,)),
            ToolMessage(content="Sunny", tool_call_id="call-1"),
            AIMessage(content="Old answer."),
        )
        recent_turn = (
            HumanMessage(content="Recent question."),
            AIMessage(content="Recent answer."),
        )
        history = (*older_turn, *recent_turn)
        builder = ContextBuilder(
            self._workspace,
            history_token_budget=estimate_messages_tokens(recent_turn),
        )

        trimmed = builder.trim_history(history)

        self.assertEqual(trimmed, recent_turn)
        self.assertLessEqual(
            estimate_messages_tokens(trimmed),
            estimate_messages_tokens(recent_turn),
        )
        self.assertNotIn(older_turn[2], trimmed)

    def test_preserves_tool_call_and_result_order_when_the_turn_fits(self) -> None:
        tool_call = ToolCallRequest(
            id="call-1",
            name="get_weather",
            arguments={"city": "Shanghai"},
        )
        turn = (
            HumanMessage(content="What is the weather?"),
            AIMessage(content="", tool_calls=(tool_call,)),
            ToolMessage(content="Sunny", tool_call_id="call-1"),
            AIMessage(content="It is sunny."),
        )
        builder = ContextBuilder(
            self._workspace,
            history_token_budget=estimate_messages_tokens(turn),
        )

        self.assertEqual(builder.trim_history(turn), turn)

    def test_current_message_is_sent_once_when_already_at_the_history_tail(self) -> None:
        current_message = HumanMessage(content="Current question.")
        builder = ContextBuilder(self._workspace)

        messages = builder.build_request_messages(
            (HumanMessage(content="Previous question."), current_message),
            current_message,
        )

        self.assertEqual(
            messages,
            (
                messages[0],
                HumanMessage(content="Previous question."),
                current_message,
            ),
        )
        self.assertEqual(messages.count(current_message), 1)

    def test_keeps_system_and_current_message_when_history_budget_is_zero(self) -> None:
        builder = ContextBuilder(self._workspace, history_token_budget=0)
        current_message = HumanMessage(content="Current question " * 20)

        messages = builder.build_request_messages(
            (HumanMessage(content="Previous question."),),
            current_message,
        )

        self.assertIsInstance(messages[0], SystemMessage)
        self.assertEqual(messages[-1], current_message)
        self.assertEqual(len(messages), 2)

    def test_token_estimate_grows_with_text_and_counts_tool_call_arguments(self) -> None:
        short_message = HumanMessage(content="short")
        long_message = HumanMessage(content="long " * 40)
        without_tool_call = AIMessage(content="Calling a tool.")
        with_tool_call = AIMessage(
            content="Calling a tool.",
            tool_calls=(
                ToolCallRequest(
                    id="call-1",
                    name="get_weather",
                    arguments={"city": "Shanghai", "units": "celsius"},
                ),
            ),
        )

        self.assertGreater(
            estimate_message_tokens(long_message),
            estimate_message_tokens(short_message),
        )
        self.assertGreater(
            estimate_message_tokens(with_tool_call),
            estimate_message_tokens(without_tool_call),
        )
        self.assertGreater(
            estimate_message_tokens(HumanMessage(content="你好你好")),
            estimate_message_tokens(HumanMessage(content="abcd")),
        )
        self.assertGreater(estimate_message_tokens(HumanMessage(content="")), 0)

    def _write(self, filename: str, content: str) -> Path:
        path = self._workspace / filename
        path.write_text(content, encoding="utf-8")
        return path
