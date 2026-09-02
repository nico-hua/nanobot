"""Focused tests for the session-scoped CronTool."""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

from nanobot.cron import CronService
from nanobot.tools import (
    RequestContext,
    ToolContext,
    bind_request_context,
)
from nanobot.tools.builtin import CronTool


class CronToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._workspace = Path(self._temporary_directory.name)
        self._service = CronService(_no_op, self._workspace)
        self._tool = CronTool.create(
            ToolContext(
                cron_service=self._service,
                cron_timezone="Asia/Shanghai",
            )
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_schema_name_and_factory_require_a_cron_service(self) -> None:
        self.assertEqual(self._tool.name, "cron")
        self.assertEqual(
            tuple(parameter.name for parameter in self._tool.parameters),
            ("action", "message", "name", "at", "every_seconds", "task_id"),
        )
        self.assertTrue(CronTool.enabled(ToolContext(cron_service=self._service)))
        self.assertFalse(CronTool.enabled(ToolContext()))
        with self.assertRaisesRegex(ValueError, "CronService"):
            CronTool.create(ToolContext())

    async def test_add_binds_the_current_request_route_to_the_task(self) -> None:
        with _bound_request():
            result = await self._tool.execute(
                action="add",
                message="Send a friendly greeting.",
                every_seconds=30,
                task_id="greeting",
            )

        task = self._service.get("greeting")
        self.assertTrue(result.success)
        self.assertEqual(result.content, "Created recurring task: greeting")
        self.assertEqual(task.name, "Scheduled task")
        self.assertEqual(task.schedule.kind, "every")
        self.assertEqual(task.schedule.every_ms, 30_000)
        self.assertEqual(task.schedule.tz, "Asia/Shanghai")
        self.assertEqual(task.payload.session_key, "session-1")
        self.assertEqual(task.payload.channel, "qq")
        self.assertEqual(task.payload.chat_id, "chat-1")
        self.assertEqual(task.payload.sender_id, "sender-1")
        self.assertEqual(task.payload.metadata, {"qq_chat_type": "c2c"})

    async def test_add_parses_at_using_the_configured_timezone(self) -> None:
        with _bound_request():
            result = await self._tool.execute(
                action="add",
                message="Send the report.",
                at="2030-01-02T03:04:05",
                task_id="report",
                name="Daily report",
            )

        task = self._service.get("report")
        self.assertTrue(result.success)
        self.assertEqual(task.name, "Daily report")
        self.assertEqual(task.schedule.kind, "at")
        self.assertEqual(task.schedule.tz, "Asia/Shanghai")
        self.assertEqual(task.schedule.at_ms, 1_893_524_645_000)

    async def test_add_validates_message_schedule_and_interval(self) -> None:
        invalid_calls = (
            {"message": "", "every_seconds": 10},
            {"message": "Reminder", "at": "2030-01-01T00:00:00", "every_seconds": 10},
            {"message": "Reminder"},
            {"message": "Reminder", "every_seconds": 0},
            {"message": "Reminder", "at": "not-a-time"},
        )

        with _bound_request():
            for arguments in invalid_calls:
                with self.subTest(arguments=arguments):
                    result = await self._tool.execute(action="add", **arguments)
                    self.assertFalse(result.success)
                    self.assertIsNotNone(result.error)

    async def test_list_only_includes_tasks_for_the_current_session(self) -> None:
        self._service.add_every(
            _seconds(60),
            task_id="current-task",
            message="Current task",
            session_key="session-1",
        )
        self._service.add_every(
            _seconds(60),
            task_id="other-task",
            message="Other task",
            session_key="session-2",
        )

        with _bound_request():
            result = await self._tool.execute(action="list")

        self.assertTrue(result.success)
        self.assertIn("current-task", result.content)
        self.assertNotIn("other-task", result.content)

    async def test_remove_rejects_tasks_from_other_sessions(self) -> None:
        self._service.add_every(
            _seconds(60),
            task_id="other-task",
            message="Other task",
            session_key="session-2",
        )

        with _bound_request():
            forbidden = await self._tool.execute(action="remove", task_id="other-task")
            missing = await self._tool.execute(action="remove", task_id="missing")

        self.assertFalse(forbidden.success)
        self.assertIn("does not belong", forbidden.error or "")
        self.assertFalse(missing.success)
        self.assertIn("not found", missing.error or "")
        self.assertIsNotNone(self._service.get("other-task"))

    async def test_remove_deletes_a_task_from_the_current_session(self) -> None:
        self._service.add_every(
            _seconds(60),
            task_id="current-task",
            message="Current task",
            session_key="session-1",
        )

        with _bound_request():
            result = await self._tool.execute(action="remove", task_id="current-task")

        self.assertTrue(result.success)
        self.assertIsNone(self._service.get("current-task"))

    async def test_tool_requires_request_context(self) -> None:
        result = await self._tool.execute(
            action="add",
            message="Reminder",
            every_seconds=10,
        )

        self.assertFalse(result.success)
        self.assertIn("user message", result.error or "")


class _bound_request:
    def __init__(self, metadata: Mapping[str, Any] | None = None) -> None:
        self._context = RequestContext(
            session_key="session-1",
            channel="qq",
            chat_id="chat-1",
            sender_id="sender-1",
            metadata=metadata or {"qq_chat_type": "c2c", "message_id": "ignored"},
        )
        self._binding = None

    def __enter__(self) -> None:
        self._binding = bind_request_context(self._context)
        self._binding.__enter__()

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._binding is not None:
            self._binding.__exit__(exc_type, exc, traceback)


def _seconds(value: float):
    return timedelta(seconds=value)


async def _no_op(task) -> None:
    del task
