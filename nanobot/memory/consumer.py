"""Background consumption of durable workspace memory events."""

from __future__ import annotations

import asyncio
import logging

from ..providers import BaseMessage
from .consolidator import MemoryConsolidationOutcome, MemoryConsolidator
from .store import MemoryStore

logger = logging.getLogger(__name__)


class MemoryEventConsumer:
    """Persist completed-turn events and process them sequentially in the background."""

    def __init__(
        self,
        store: MemoryStore,
        consolidator: MemoryConsolidator,
    ) -> None:
        if not isinstance(store, MemoryStore):
            raise TypeError("MemoryEventConsumer requires a MemoryStore")
        if not isinstance(consolidator, MemoryConsolidator):
            raise TypeError("MemoryEventConsumer requires a MemoryConsolidator")

        self._store = store
        self._consolidator = consolidator
        self._task: asyncio.Task[None] | None = None
        self._wake_requested = False
        self._closed = False

    def append(self, session_key: str, messages: tuple[BaseMessage, ...]) -> None:
        """Durably append one completed-turn snapshot and wake the consumer."""

        if self._closed:
            return
        try:
            self._store.append_event(session_key, messages)
        except Exception:
            logger.exception("Unable to append a long-term memory event")
            return
        self.wake()

    def wake(self) -> None:
        """Ensure one background task processes events after the current cursor."""

        if self._closed:
            return
        self._wake_requested = True
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._consume_pending_events())
        self._task.add_done_callback(self._on_task_done)

    async def wait(self) -> None:
        """Wait until the current consumer task and any queued wake-up complete."""

        while self._task is not None:
            task = self._task
            await asyncio.gather(task, return_exceptions=True)
            if self._task is task:
                self._on_task_done(task)

    async def close(self) -> None:
        """Cancel the consumer task once and wait for its cleanup."""

        if self._closed:
            return
        self._closed = True
        self._wake_requested = False
        task = self._task
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._task = None

    async def _consume_pending_events(self) -> None:
        try:
            while True:
                self._wake_requested = False
                cursor = self._store.read_cursor()
                events = self._store.read_events_after(cursor)
                if not events:
                    return
                outcome = await self._consolidator.consolidate_events(events)
                if outcome is MemoryConsolidationOutcome.FAILED:
                    logger.warning(
                        "Long-term memory event batch consolidation failed "
                        "(first_event_id=%d, last_event_id=%d, count=%d)",
                        events[0].event_id,
                        events[-1].event_id,
                        len(events),
                    )
                    return
                # A successful update or intentional empty result means every
                # event in this batch is fully processed. Only failures retry.
                self._store.update_cursor(events[-1].event_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background long-term memory event processing failed")

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        if self._task is task:
            self._task = None
        if self._wake_requested and not self._closed:
            self.wake()
