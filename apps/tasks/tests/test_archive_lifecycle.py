"""Soft delete has to hide things and let them come back."""

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.boards.models import BoardMembership
from apps.tasks.models import Task
from taskflow.testing import (
    create_board,
    create_board_list,
    create_task,
    create_user,
    create_workspace,
    results,
)


class ArchivedListHidesItsTasksTests(APITestCase):
    """Archiving a list used to hide only the list."""

    def setUp(self):
        self.owner = create_user("owner@example.com")
        self.workspace = create_workspace(owner=self.owner)
        self.board = create_board(workspace=self.workspace, creator=self.owner)
        self.board_list = create_board_list(board=self.board)
        self.task = create_task(board_list=self.board_list, creator=self.owner)
        self.client.force_authenticate(self.owner)
        self.client.delete(
            reverse(
                "board-list-detail",
                args=[self.workspace.id, self.board.id, self.board_list.id],
            )
        )

    def detail_url(self):
        return reverse("task-detail", args=[self.workspace.id, self.task.id])

    def test_the_task_cannot_be_read(self):
        self.assertEqual(self.client.get(self.detail_url()).status_code, 404)

    def test_the_task_cannot_be_edited(self):
        response = self.client.patch(self.detail_url(), {"title": "New"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Task")

    def test_the_task_cannot_be_commented_on(self):
        response = self.client.post(
            reverse("task-comment-create", args=[self.workspace.id, self.task.id]),
            {"body": "Still here?"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_the_task_cannot_be_moved(self):
        response = self.client.post(
            reverse("task-move", args=[self.workspace.id, self.task.id]),
            {"target_list_id": self.board_list.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_restoring_the_list_brings_the_task_back(self):
        self.client.post(
            reverse(
                "board-list-restore",
                args=[self.workspace.id, self.board.id, self.board_list.id],
            )
        )

        response = self.client.get(self.detail_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # The task's own flag was never touched, so it comes back as it was.
        self.task.refresh_from_db()
        self.assertFalse(self.task.is_archived)


class ArchiveRoundTripTests(APITestCase):
    def setUp(self):
        self.owner = create_user("owner@example.com")
        self.workspace = create_workspace(owner=self.owner)
        self.board = create_board(workspace=self.workspace, creator=self.owner)
        self.board_list = create_board_list(board=self.board)
        self.task = create_task(board_list=self.board_list, creator=self.owner)
        self.client.force_authenticate(self.owner)

    def test_an_archived_task_is_listed_and_restored(self):
        listing = reverse("task-list-create", args=[self.workspace.id, self.board_list.id])
        self.client.delete(reverse("task-detail", args=[self.workspace.id, self.task.id]))

        active = self.client.get(listing)
        archived = self.client.get(listing, {"archived": "true"})
        restored = self.client.post(reverse("task-restore", args=[self.workspace.id, self.task.id]))

        self.assertEqual(results(active), [])
        self.assertEqual(len(results(archived)), 1)
        self.assertEqual(restored.status_code, status.HTTP_200_OK)
        self.assertEqual(len(results(self.client.get(listing))), 1)

    def test_a_restored_task_goes_to_the_end_of_its_list(self):
        other = create_task(board_list=self.board_list, creator=self.owner, title="Other")
        Task.objects.filter(pk=other.pk).update(position=self.task.position * 2)
        self.client.delete(reverse("task-detail", args=[self.workspace.id, self.task.id]))

        self.client.post(reverse("task-restore", args=[self.workspace.id, self.task.id]))

        self.task.refresh_from_db()
        other.refresh_from_db()
        # Its old position may now sit between rows that have since moved.
        self.assertGreater(self.task.position, other.position)

    def test_an_archived_list_is_listed_and_restored(self):
        listing = reverse("board-list-list-create", args=[self.workspace.id, self.board.id])
        detail = reverse(
            "board-list-detail",
            args=[self.workspace.id, self.board.id, self.board_list.id],
        )
        self.client.delete(detail)

        archived = self.client.get(listing, {"archived": "true"})
        restored = self.client.post(
            reverse(
                "board-list-restore",
                args=[self.workspace.id, self.board.id, self.board_list.id],
            )
        )

        self.assertEqual(len(results(archived)), 1)
        self.assertEqual(restored.status_code, status.HTTP_200_OK)
        self.board_list.refresh_from_db()
        self.assertFalse(self.board_list.is_archived)

    def test_a_viewer_cannot_restore(self):
        viewer = create_user("viewer@example.com")
        BoardMembership.objects.create(
            board=self.board, user=viewer, role=BoardMembership.Role.VIEWER
        )
        self.client.delete(reverse("task-detail", args=[self.workspace.id, self.task.id]))
        self.client.force_authenticate(viewer)

        response = self.client.post(reverse("task-restore", args=[self.workspace.id, self.task.id]))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.task.refresh_from_db()
        self.assertTrue(self.task.is_archived)


class TaskUpdateQueryCountTests(APITestCase):
    """Editing a task used to cost one query pair per assignee, twice over."""

    def setUp(self):
        self.owner = create_user("owner@example.com")
        self.workspace = create_workspace(owner=self.owner)
        self.board = create_board(workspace=self.workspace, creator=self.owner)
        self.board_list = create_board_list(board=self.board)
        self.task = create_task(board_list=self.board_list, creator=self.owner)
        self.client.force_authenticate(self.owner)
        for index in range(19):
            BoardMembership.objects.create(
                board=self.board,
                user=create_user(f"member{index}@example.com"),
                role=BoardMembership.Role.MEMBER,
            )
        self.membership_ids = list(
            BoardMembership.objects.filter(board=self.board).values_list("id", flat=True)
        )

    def test_the_query_count_does_not_grow_with_the_assignee_count(self):
        url = reverse("task-detail", args=[self.workspace.id, self.task.id])
        # Django skips the follow-up prefetches when the first one is empty.
        self.client.patch(url, {"assignee_membership_ids": self.membership_ids[:1]}, format="json")

        with CaptureQueriesContext(connection) as few:
            self.client.patch(
                url, {"assignee_membership_ids": self.membership_ids[:2]}, format="json"
            )
        with CaptureQueriesContext(connection) as many:
            response = self.client.patch(
                url, {"assignee_membership_ids": self.membership_ids}, format="json"
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["assignees"]), 20)
        self.assertEqual(len(few.captured_queries), len(many.captured_queries))
