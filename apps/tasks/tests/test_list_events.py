"""List mutations have to reach other viewers of the same board."""

from unittest.mock import patch

from django.urls import reverse
from rest_framework.test import APITestCase

from taskflow.testing import (
    create_board,
    create_board_list,
    create_user,
    create_workspace,
)


class BoardListEventTests(APITestCase):
    def setUp(self):
        self.owner = create_user("owner@example.com")
        self.workspace = create_workspace(owner=self.owner)
        self.board = create_board(workspace=self.workspace, creator=self.owner)
        self.board_list = create_board_list(board=self.board, position=1000)
        self.client.force_authenticate(self.owner)

    def published_events(self, request):
        """Run `request` and return the payloads it put on the channel layer.

        The events go through `transaction.on_commit`, which TestCase never
        reaches on its own.
        """
        with patch("apps.tasks.realtime.async_to_sync") as async_to_sync:
            sender = async_to_sync.return_value
            with self.captureOnCommitCallbacks(execute=True):
                response = request()
        return response, [call.args[1]["payload"] for call in sender.call_args_list]

    def test_creating_a_list_is_announced(self):
        response, events = self.published_events(
            lambda: self.client.post(
                reverse("board-list-list-create", args=[self.workspace.id, self.board.id]),
                {"name": "Doing"},
                format="json",
            )
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual([event["event_type"] for event in events], ["list.created"])
        self.assertEqual(events[0]["board_id"], self.board.id)
        self.assertEqual(events[0]["list_id"], response.data["id"])
        self.assertIsNone(events[0]["task_id"])

    def test_renaming_a_list_is_announced(self):
        response, events = self.published_events(
            lambda: self.client.patch(
                reverse(
                    "board-list-detail",
                    args=[self.workspace.id, self.board.id, self.board_list.id],
                ),
                {"name": "Renamed"},
                format="json",
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([event["event_type"] for event in events], ["list.updated"])

    def test_archiving_a_list_is_announced(self):
        response, events = self.published_events(
            lambda: self.client.delete(
                reverse(
                    "board-list-detail",
                    args=[self.workspace.id, self.board.id, self.board_list.id],
                )
            )
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual([event["event_type"] for event in events], ["list.archived"])

    def test_restoring_a_list_is_announced(self):
        self.client.delete(
            reverse(
                "board-list-detail",
                args=[self.workspace.id, self.board.id, self.board_list.id],
            )
        )

        response, events = self.published_events(
            lambda: self.client.post(
                reverse(
                    "board-list-restore",
                    args=[self.workspace.id, self.board.id, self.board_list.id],
                )
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([event["event_type"] for event in events], ["list.restored"])

    def test_moving_a_list_is_announced(self):
        anchor = create_board_list(board=self.board, name="Done", position=2000)

        response, events = self.published_events(
            lambda: self.client.post(
                reverse(
                    "board-list-move",
                    args=[self.workspace.id, self.board.id, anchor.id],
                ),
                {"before_list_id": self.board_list.id},
                format="json",
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([event["event_type"] for event in events], ["list.moved"])

    def test_restoring_a_task_is_announced(self):
        from taskflow.testing import create_task

        task = create_task(board_list=self.board_list, creator=self.owner)
        self.client.delete(reverse("task-detail", args=[self.workspace.id, task.id]))

        response, events = self.published_events(
            lambda: self.client.post(reverse("task-restore", args=[self.workspace.id, task.id]))
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([event["event_type"] for event in events], ["task.restored"])
        self.assertEqual(events[0]["task_id"], task.id)
