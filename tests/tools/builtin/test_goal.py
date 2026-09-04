"""Focused tests for mode-scoped sustained-goal tools."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nanobot.agent.commands import CommandRouter
from nanobot.bus import InboundMessage, MessageBus
from nanobot.session import SessionManager
from nanobot.tools import RequestContext, ToolContext, bind_request_context
from nanobot.tools.builtin import CreateGoalTool, UpdateGoalTool


class GoalToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._session_manager = SessionManager(Path(self._temporary_directory.name))
        self._message_bus = MessageBus()
        context = ToolContext(
            session_manager=self._session_manager,
            message_bus=self._message_bus,
        )
        self._create_tool = CreateGoalTool.create(context)
        self._update_tool = UpdateGoalTool.create(context)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_create_goal_succeeds_only_outside_goal_mode(self) -> None:
        with _bound_request("session-one", is_goal_mode=False):
            result = await self._create_tool.execute("Finish the migration.")

        saved = self._session_manager.get_or_create("session-one")
        self.assertTrue(result.success)
        self.assertEqual(
            result.content,
            "Goal created and scheduled: Finish the migration.\n\n"
            "Its progress will continue in a separate run.",
        )
        self.assertIsNotNone(saved.goal_state)
        self.assertEqual(saved.goal_state.status, "active")
        self.assertEqual(saved.goal_state.objective, "Finish the migration.")
        goal_message = await self._message_bus.consume_inbound()
        self.assertEqual(goal_message.channel, "test")
        self.assertEqual(goal_message.chat_id, "chat")
        self.assertEqual(goal_message.sender_id, "sender")
        self.assertEqual(goal_message.session_id, "session-one")
        self.assertEqual(goal_message.metadata, {"source": "goal"})
        self.assertIn("Finish the migration.", goal_message.content)

        before = saved.goal_state
        with _bound_request("session-one", is_goal_mode=True):
            rejected = await self._create_tool.execute("Another goal")

        self.assertFalse(rejected.success)
        self.assertIn("outside goal mode", rejected.error or "")
        self.assertEqual(
            self._session_manager.get_or_create("session-one").goal_state,
            before,
        )

    async def test_update_goal_succeeds_only_during_goal_mode(self) -> None:
        original = self._session_manager.create_goal(
            self._session_manager.get_or_create("session-one"),
            "Initial objective.",
        )
        before = original.goal_state

        with _bound_request("session-one", is_goal_mode=False):
            rejected = await self._update_tool.execute(
                action="update",
                objective="Replacement objective.",
            )

        self.assertFalse(rejected.success)
        self.assertIn("during goal mode", rejected.error or "")
        self.assertEqual(
            self._session_manager.get_or_create("session-one").goal_state,
            before,
        )

        with _bound_request("session-one", is_goal_mode=True):
            updated = await self._update_tool.execute(
                action="update",
                objective="Replacement objective.",
            )

        saved = self._session_manager.get_or_create("session-one")
        self.assertTrue(updated.success)
        self.assertEqual(saved.goal_state.status if saved.goal_state else None, "active")
        self.assertEqual(
            saved.goal_state.objective if saved.goal_state else None,
            "Replacement objective.",
        )

    async def test_create_goal_reports_a_message_bus_failure(self) -> None:
        tool = CreateGoalTool.create(
            ToolContext(
                session_manager=self._session_manager,
                message_bus=FailingMessageBus(),
            )
        )

        with _bound_request("session-one", is_goal_mode=False):
            result = await tool.execute("Finish the migration.")

        self.assertFalse(result.success)
        self.assertEqual(
            result.error,
            "Goal was created but could not be scheduled: inbound queue unavailable",
        )
        saved = self._session_manager.get_or_create("session-one")
        self.assertEqual(saved.goal_state.objective if saved.goal_state else None, "Finish the migration.")

    async def test_goal_sessions_are_isolated_and_cancellation_is_persisted(self) -> None:
        self._session_manager.create_goal(
            self._session_manager.get_or_create("session-one"),
            "First objective.",
        )
        self._session_manager.create_goal(
            self._session_manager.get_or_create("session-two"),
            "Second objective.",
        )

        with _bound_request("session-one", is_goal_mode=True):
            result = await self._update_tool.execute(action="stop")

        first = self._session_manager.get_or_create("session-one").goal_state
        second = self._session_manager.get_or_create("session-two").goal_state
        self.assertTrue(result.success)
        self.assertEqual(first.status if first else None, "cancelled")
        self.assertEqual(second.status if second else None, "active")
        self.assertEqual(second.objective if second else None, "Second objective.")

    async def test_slash_goal_and_create_goal_share_the_same_state_format(self) -> None:
        router = CommandRouter(self._session_manager)
        message = InboundMessage(
            channel="test",
            chat_id="chat-command",
            sender_id="sender",
            session_id="session-command",
            content="/goal Command objective.",
        )
        command_session = self._session_manager.get_or_create("session-command")
        reply = await router.route(message, "session-command", command_session)

        with _bound_request("session-tool", is_goal_mode=False):
            tool_result = await self._create_tool.execute("Tool objective.")

        command_goal = self._session_manager.get_or_create("session-command").goal_state
        tool_goal = self._session_manager.get_or_create("session-tool").goal_state
        self.assertIsNotNone(reply)
        self.assertTrue(tool_result.success)
        self.assertIsNotNone(command_goal)
        self.assertIsNotNone(tool_goal)
        self.assertEqual(command_goal.status, tool_goal.status)
        self.assertEqual(set(command_goal.to_dict()), set(tool_goal.to_dict()))

    async def test_factory_requires_a_session_manager(self) -> None:
        self.assertTrue(
            CreateGoalTool.enabled(
                ToolContext(
                    session_manager=self._session_manager,
                    message_bus=self._message_bus,
                )
            )
        )
        self.assertIn("schedule", self._create_tool.description)
        self.assertTrue(
            UpdateGoalTool.enabled(ToolContext(session_manager=self._session_manager))
        )
        self.assertFalse(CreateGoalTool.enabled(ToolContext(session_manager=self._session_manager)))
        self.assertFalse(UpdateGoalTool.enabled(ToolContext()))
        with self.assertRaisesRegex(ValueError, "SessionManager and MessageBus"):
            CreateGoalTool.create(ToolContext())
        with self.assertRaisesRegex(ValueError, "SessionManager"):
            UpdateGoalTool.create(ToolContext())


class FailingMessageBus(MessageBus):
    async def publish_inbound(self, message: InboundMessage) -> None:
        del message
        raise RuntimeError("inbound queue unavailable")


class _bound_request:
    def __init__(self, session_key: str, *, is_goal_mode: bool) -> None:
        self._binding = bind_request_context(
            RequestContext(
                session_key=session_key,
                channel="test",
                chat_id="chat",
                sender_id="sender",
                is_goal_mode=is_goal_mode,
            )
        )

    def __enter__(self) -> None:
        self._binding.__enter__()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._binding.__exit__(exc_type, exc, traceback)
