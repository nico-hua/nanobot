"""Focused tests for workspace Cron task persistence and restart recovery."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from nanobot.cron import (
    CronJobState,
    CronPayload,
    CronSchedule,
    CronService,
    CronStorageError,
    CronTask,
    JsonCronTaskStorage,
)


class CronPersistenceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._workspace = self._temporary_directory.name
        self._services: list[CronService] = []

    async def asyncTearDown(self) -> None:
        for service in self._services:
            await service.stop()
        self._temporary_directory.cleanup()

    async def test_added_task_is_restored_from_json(self) -> None:
        service = self._service(_no_op)
        service.add_every(
            timedelta(minutes=5),
            task_id="standup",
            name="Daily standup",
            start_at=datetime.now(timezone.utc) + timedelta(minutes=1),
            message="Prepare the standup update.",
            session_key="session-1",
            channel="fake",
            chat_id="chat-1",
            metadata={"delivery_type": "test"},
        )

        restored_service = self._service(_no_op)
        await restored_service.start()
        restored = restored_service.get("standup")

        self.assertTrue(service.storage_path.is_file())
        self.assertEqual(restored.name, "Daily standup")
        self.assertEqual(restored.payload.message, "Prepare the standup update.")
        self.assertEqual(restored.payload.session_key, "session-1")
        self.assertEqual(restored.payload.channel, "fake")
        self.assertEqual(restored.payload.metadata, {"delivery_type": "test"})
        self.assertEqual(restored.schedule.kind, "every")
        self.assertEqual(restored.schedule.every_ms, 300_000)
        self.assertEqual(restored.schedule.tz, "Asia/Shanghai")
        document = json.loads(service.storage_path.read_text(encoding="utf-8"))
        self.assertEqual(document["version"], 1)
        self.assertIsInstance(document["tasks"][0]["state"]["next_run_at"], int)

    async def test_deleted_task_is_not_restored(self) -> None:
        service = self._service(_no_op)
        service.add_at(
            datetime.now(timezone.utc) + timedelta(minutes=1),
            task_id="obsolete",
        )
        service.remove("obsolete")

        restored_service = self._service(_no_op)
        await restored_service.start()

        self.assertIsNone(restored_service.get("obsolete"))

    async def test_completed_one_time_task_state_is_restored_without_reexecution(self) -> None:
        completed = asyncio.Event()

        async def callback(task: CronTask) -> None:
            del task
            completed.set()

        service = self._service(callback)
        service.add_at(
            datetime.now(timezone.utc) + timedelta(milliseconds=20),
            task_id="once",
        )
        await service.start()
        await asyncio.wait_for(completed.wait(), timeout=1)
        await service.stop()

        called_after_restart = asyncio.Event()

        async def restarted_callback(task: CronTask) -> None:
            del task
            called_after_restart.set()

        restored_service = self._service(restarted_callback)
        await restored_service.start()
        await asyncio.sleep(0.05)
        restored = restored_service.get("once")

        self.assertFalse(called_after_restart.is_set())
        self.assertFalse(restored.enabled)
        self.assertIsNone(restored.state.next_run_at)
        self.assertIsNotNone(restored.state.last_run_at)
        self.assertIsInstance(restored.state.last_run_at, int)
        self.assertEqual(restored.state.last_status, "success")

    async def test_periodic_next_run_is_restored_without_recalculation(self) -> None:
        service = self._service(_no_op)
        task = service.add_every(
            timedelta(minutes=5),
            task_id="repeat",
            start_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        )
        expected_next_run = task.state.next_run_at
        await service.stop()

        restored_service = self._service(_no_op)
        await restored_service.start()

        self.assertEqual(restored_service.get("repeat").state.next_run_at, expected_next_run)

    async def test_overdue_periodic_task_runs_once_after_restart(self) -> None:
        service = self._service(_no_op)
        service.add_every(
            timedelta(minutes=5),
            task_id="repeat",
            start_at=datetime.now(timezone.utc) + timedelta(milliseconds=100),
        )
        await service.start()
        await service.stop()
        await asyncio.sleep(0.12)

        called = asyncio.Event()

        async def callback(task: CronTask) -> None:
            del task
            called.set()

        restored_service = self._service(callback)
        await restored_service.start()
        await asyncio.wait_for(called.wait(), timeout=1)

        restored = restored_service.get("repeat")
        self.assertTrue(restored.enabled)
        self.assertIsNotNone(restored.state.last_run_at)
        self.assertGreater(restored.state.next_run_at, restored.state.last_run_at)

    async def test_invalid_json_is_not_overwritten_when_starting(self) -> None:
        task_path = Path(self._workspace) / "cron" / "tasks.json"
        task_path.parent.mkdir(parents=True)
        invalid_content = "{not valid json"
        task_path.write_text(invalid_content, encoding="utf-8")

        service = self._service(_no_op)

        with self.assertRaisesRegex(CronStorageError, "invalid JSON"):
            await service.start()
        await service.stop()

        self.assertEqual(task_path.read_text(encoding="utf-8"), invalid_content)

    def _service(self, callback) -> CronService:
        service = CronService(callback, self._workspace)
        self._services.append(service)
        return service


class JsonCronTaskStorageTest(unittest.TestCase):
    def test_failed_atomic_write_keeps_the_previous_target_file_intact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            storage = JsonCronTaskStorage(temporary_directory)
            original = _task("original")
            storage.save((original,))
            original_content = storage.path.read_text(encoding="utf-8")

            with patch("nanobot.cron.storage.os.replace", side_effect=OSError("disk error")):
                with self.assertRaisesRegex(CronStorageError, "Unable to save"):
                    storage.save((_task("replacement"),))

            self.assertEqual(storage.path.read_text(encoding="utf-8"), original_content)
            self.assertEqual(tuple(storage.path.parent.glob(".tasks-*.tmp")), ())


async def _no_op(task: CronTask) -> None:
    del task


def _task(task_id: str) -> CronTask:
    when = datetime.now(timezone.utc) + timedelta(minutes=1)
    return CronTask(
        id=task_id,
        schedule=CronSchedule(
            kind="at",
            at_ms=round(when.timestamp() * 1000),
        ),
        payload=CronPayload(
            message="A task message.",
            session_key="session-1",
        ),
        name=task_id,
        state=CronJobState(next_run_at=round(when.timestamp() * 1000)),
    )
