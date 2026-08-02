"""Board event fan-out; a board is the unit a websocket subscribes to."""

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction

from .consumers import board_task_group_name
from .models import BoardList, Task


logger = logging.getLogger(__name__)


def _publish(*, board_id: int, payload: dict) -> None:
    """Queue one event, delivered only if the surrounding transaction commits."""

    def send() -> None:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        try:
            async_to_sync(channel_layer.group_send)(
                board_task_group_name(board_id),
                {"type": "task.event", "payload": payload},
            )
        except Exception:
            logger.exception("Unable to publish board event", extra={"board_id": board_id})

    transaction.on_commit(send)


def publish_task_event(*, task: Task, event_type: str, actor_id: int | None) -> None:
    board = task.board_list.board
    _publish(
        board_id=board.id,
        payload={
            "type": "task.event",
            "event_type": event_type,
            "workspace_id": board.workspace_id,
            "board_id": board.id,
            "list_id": task.board_list_id,
            "task_id": task.id,
            "actor_id": actor_id,
        },
    )


def publish_board_list_event(
    *,
    board_list: BoardList,
    event_type: str,
    actor_id: int | None,
) -> None:
    """Announce a column change."""
    board = board_list.board
    _publish(
        board_id=board.id,
        payload={
            "type": "task.event",
            "event_type": event_type,
            "workspace_id": board.workspace_id,
            "board_id": board.id,
            "list_id": board_list.id,
            "task_id": None,
            "actor_id": actor_id,
        },
    )
