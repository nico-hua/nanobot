"""Adapt due Cron tasks into inbound Agent messages."""

from __future__ import annotations

from ..bus import InboundMessage, MessageBus
from .models import CronTask

_CRON_MESSAGE_TEMPLATE = (
    "The scheduled time has arrived. Execute this scheduled cron job now and "
    "report the result to the user in the same session.\n\n"
    "Rules:\n\n"
    "- Speak directly to the user in their language.\n"
    "- Do not narrate internal progress.\n"
    "- Do not include user IDs.\n"
    "- Do not add status reports like \"Done\" or \"Reminded\" unless they are "
    "the natural response.\n\n"
    "Cron job: {message}"
)


class CronMessagePublisher:
    """Publish one route-aware Cron task through the shared MessageBus."""

    def __init__(self, message_bus: MessageBus) -> None:
        if not isinstance(message_bus, MessageBus):
            raise TypeError("CronMessagePublisher requires a MessageBus")
        self._message_bus = message_bus

    async def publish(self, task: CronTask) -> None:
        """Convert a due task and publish it as a normal inbound Agent message."""

        await self._message_bus.publish_inbound(cron_task_to_inbound_message(task))


def cron_task_to_inbound_message(task: CronTask) -> InboundMessage:
    """Return the normal inbound message represented by one route-aware task."""

    if not isinstance(task, CronTask):
        raise TypeError("Cron message publishing requires a CronTask")
    channel = _required_task_field(task.payload.channel, "channel")
    chat_id = _required_task_field(task.payload.chat_id, "chat_id")
    sender_id = _required_task_field(task.payload.sender_id, "sender_id")
    session_key = _optional_session_key(task.payload.session_key)
    message = _required_task_field(task.payload.message, "message")
    metadata = dict(task.payload.metadata)
    metadata.update(
        {
            "source": "cron",
            "cron_task_id": task.id,
        }
    )
    return InboundMessage(
        channel=channel,
        chat_id=chat_id,
        sender_id=sender_id,
        session_id=session_key,
        content=_CRON_MESSAGE_TEMPLATE.format(message=message),
        metadata=metadata,
    )


def _required_task_field(value: str | None, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Cron task requires a non-empty {name}")
    return value


def _optional_session_key(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Cron task requires a string session_key")  # noqa: TRY004
    return value
