"""Focused tests for the in-memory CronService."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from nanobot.cron import CronService, CronTask


class CronServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._workspace = self._temporary_directory.name

    async def asyncTearDown(self) -> None:
        if hasattr(self, "_service"):
            await self._service.stop()
        self._temporary_directory.cleanup()

    async def test_adds_and_queries_persistable_tasks(self) -> None:
        self._service = CronService(_no_op, self._workspace)
        when = datetime.now(timezone.utc) + timedelta(minutes=1)

        task = self._service.add_at(
            when,
            task_id="once",
            message="Send a reminder.",
            session_key="session-1",
            channel="fake",
            chat_id="chat-1",
            metadata={"delivery_type": "test"},
        )

        self.assertEqual(task.id, "once")
        self.assertEqual(task.schedule.kind, "at")
        self.assertEqual(task.schedule.at_ms, round(when.timestamp() * 1000))
        self.assertEqual(task.schedule.tz, "Asia/Shanghai")
        self.assertEqual(task.state.next_run_at, task.schedule.at_ms)
        self.assertEqual(task.payload.message, "Send a reminder.")
        self.assertEqual(task.payload.session_key, "session-1")
        self.assertEqual(task.payload.sender_id, "cron")
        self.assertEqual(task.payload.metadata, {"delivery_type": "test"})
        self.assertTrue(task.enabled)
        self.assertIsNone(task.state.last_run_at)
        self.assertIs(self._service.get("once"), task)
        self.assertEqual(self._service.list_tasks(), (task,))

    async def test_removes_tasks(self) -> None:
        self._service = CronService(_no_op, self._workspace)
        self._service.add_every(timedelta(seconds=1), task_id="repeat")

        self.assertTrue(self._service.remove("repeat"))
        self.assertFalse(self._service.remove("repeat"))
        self.assertIsNone(self._service.get("repeat"))
        self.assertEqual(self._service.list_tasks(), ())

    async def test_rejects_missing_or_invalid_service_callback(self) -> None:
        with self.assertRaisesRegex(TypeError, "callback"):
            CronService(None, self._workspace)  # type: ignore[arg-type]

    async def test_rejects_naive_task_times(self) -> None:
        self._service = CronService(_no_op, self._workspace)

        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self._service.add_at(datetime.now())
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self._service.add_every(
                timedelta(seconds=1),
                start_at=datetime.now(),
            )

    async def test_runs_a_one_time_task_once_and_records_its_state(self) -> None:
        calls: list[CronTask] = []
        called = asyncio.Event()

        async def callback(task: CronTask) -> None:
            calls.append(task)
            called.set()

        self._service = CronService(callback, self._workspace)
        self._service.add_at(
            datetime.now(timezone.utc) + timedelta(milliseconds=20),
            task_id="once",
        )
        await self._service.start()
        await asyncio.wait_for(called.wait(), timeout=1)
        await asyncio.sleep(0.05)

        self.assertEqual(tuple(task.id for task in calls), ("once",))
        task = self._service.get("once")
        self.assertIsNotNone(task)
        self.assertFalse(task.enabled)
        self.assertIsNone(task.state.next_run_at)
        self.assertIsNotNone(task.state.last_run_at)
        self.assertIsNone(task.state.last_error)

    async def test_runs_periodic_task_repeatedly_with_the_shared_callback(self) -> None:
        calls: list[CronTask] = []
        called_twice = asyncio.Event()

        async def callback(task: CronTask) -> None:
            calls.append(task)
            if len(calls) == 2:
                called_twice.set()

        self._service = CronService(callback, self._workspace)
        self._service.add_every(
            timedelta(milliseconds=15),
            task_id="repeat",
        )
        await self._service.start()
        await asyncio.wait_for(called_twice.wait(), timeout=1)

        self.assertGreaterEqual(len(calls), 2)
        task = self._service.get("repeat")
        self.assertEqual(task.schedule.kind, "every")
        self.assertIsNotNone(task.state.last_run_at)
        self.assertIsNotNone(task.state.next_run_at)
        self.assertIsNone(task.state.last_error)

    async def test_callback_failure_does_not_stop_other_tasks_and_is_recorded(self) -> None:
        successful_call = asyncio.Event()

        async def callback(task: CronTask) -> None:
            if task.id == "failing":
                raise RuntimeError("expected callback failure")
            successful_call.set()

        self._service = CronService(callback, self._workspace)
        due_at = datetime.now(timezone.utc) + timedelta(milliseconds=20)
        self._service.add_at(due_at, task_id="failing")
        self._service.add_at(due_at, task_id="successful")
        await self._service.start()
        await asyncio.wait_for(successful_call.wait(), timeout=1)

        self.assertTrue(self._service.is_running)
        failing = self._service.get("failing")
        successful = self._service.get("successful")
        self.assertFalse(failing.enabled)
        self.assertEqual(failing.state.last_error, "RuntimeError")
        self.assertFalse(successful.enabled)
        self.assertIsNone(successful.state.last_error)

    async def test_start_and_stop_manage_one_background_scheduler(self) -> None:
        self._service = CronService(_no_op, self._workspace)

        await self._service.start()
        self.assertTrue(self._service.is_running)
        await self._service.start()
        self.assertTrue(self._service.is_running)
        await self._service.stop()

        self.assertFalse(self._service.is_running)
        await self._service.stop()

    async def test_waits_without_enabled_tasks_until_stopped_or_woken(self) -> None:
        self._service = CronService(_no_op, self._workspace)

        await self._service.start()
        await asyncio.sleep(0.02)

        self.assertTrue(self._service.is_running)


async def _no_op(task: CronTask) -> None:
    del task
