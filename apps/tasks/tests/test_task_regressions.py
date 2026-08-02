"""Regression coverage for defects found in the task API review."""

from django.urls import reverse
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from apps.boards.models import BoardMembership
from apps.tasks.models import BoardList, Task, TaskActivity, TaskAssignment, TaskComment
from apps.tasks.services import move_board_list, move_task, replace_task_assignments
from taskflow.testing import (
    add_board_member,
    add_workspace_member,
    create_board,
    create_board_list,
    create_task,
    create_user,
    create_workspace,
    results,
)


class MoveAnchorTests(APITestCase):
    """A vanished insert-anchor used to escape as a bare StopIteration (500)."""

    def setUp(self):
        self.owner = create_user("owner@example.com")
        self.workspace = create_workspace(owner=self.owner)
        self.board = create_board(workspace=self.workspace, creator=self.owner)
        self.todo = create_board_list(board=self.board, name="Todo", position=1000)
        self.doing = create_board_list(board=self.board, name="Doing", position=2000)

    def test_moving_before_a_concurrently_archived_task_raises_validation_error(self):
        moving = create_task(board_list=self.todo, creator=self.owner, title="Move")
        anchor = create_task(board_list=self.doing, creator=self.owner, title="Anchor")
        # The anchor is archived after the view resolved it.
        Task.objects.filter(pk=anchor.pk).update(is_archived=True)

        with self.assertRaises(ValidationError) as raised:
            move_task(task=moving, target_board_list=self.doing, before_task=anchor)

        self.assertIn("before_task_id", raised.exception.detail)
        moving.refresh_from_db()
        self.assertEqual(moving.board_list_id, self.todo.id)

    def test_moving_before_a_concurrently_archived_list_raises_validation_error(self):
        third = create_board_list(board=self.board, name="Done", position=3000)
        BoardList.objects.filter(pk=self.doing.pk).update(is_archived=True)

        with self.assertRaises(ValidationError) as raised:
            move_board_list(board_list=third, before_board_list=self.doing)

        self.assertIn("before_list_id", raised.exception.detail)


class ReplaceAssignmentsTests(APITestCase):
    """replace_task_assignments assumed the task came from a prefetched queryset."""

    def setUp(self):
        self.owner = create_user("owner@example.com")
        self.workspace = create_workspace(owner=self.owner)
        self.board = create_board(workspace=self.workspace, creator=self.owner)
        self.board_list = create_board_list(board=self.board)

    def test_works_on_a_task_loaded_without_prefetch_related(self):
        task = create_task(board_list=self.board_list, creator=self.owner)
        membership = BoardMembership.objects.get(board=self.board, user=self.owner)
        unprefetched = Task.objects.get(pk=task.pk)

        replace_task_assignments(
            task=unprefetched,
            assignee_membership_ids=[membership.id],
        )

        self.assertEqual(
            list(task.assignments.values_list("membership_id", flat=True)),
            [membership.id],
        )

    def test_clears_a_stale_prefetch_cache_when_there_is_one(self):
        task = create_task(board_list=self.board_list, creator=self.owner)
        membership = BoardMembership.objects.get(board=self.board, user=self.owner)
        prefetched = Task.objects.prefetch_related("assignments").get(pk=task.pk)
        self.assertEqual(list(prefetched.assignments.all()), [])

        replace_task_assignments(
            task=prefetched,
            assignee_membership_ids=[membership.id],
        )

        self.assertEqual(len(prefetched.assignments.all()), 1)


class CommentDeletionTests(APITestCase):
    """Deleting a comment must take its timeline entry with it, atomically."""

    def setUp(self):
        self.owner = create_user("owner@example.com")
        self.workspace = create_workspace(owner=self.owner)
        self.board = create_board(workspace=self.workspace, creator=self.owner)
        self.board_list = create_board_list(board=self.board)
        self.task = create_task(board_list=self.board_list, creator=self.owner)
        self.client.force_authenticate(self.owner)

    def test_deleting_a_comment_removes_its_activity_entry(self):
        created = self.client.post(
            reverse("task-comment-create", args=[self.workspace.id, self.task.id]),
            {"body": "Looks good"},
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            TaskActivity.objects.filter(
                task=self.task,
                action=TaskActivity.Action.COMMENT_ADDED,
            ).count(),
            1,
        )

        response = self.client.delete(
            reverse("task-comment-detail", args=[self.workspace.id, created.data["id"]])
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(TaskComment.objects.filter(pk=created.data["id"]).exists())
        self.assertFalse(
            TaskActivity.objects.filter(
                task=self.task,
                action=TaskActivity.Action.COMMENT_ADDED,
            ).exists()
        )


class CommentVisibilityTests(APITestCase):
    """Authorship gates writing a comment, not reading one."""

    def setUp(self):
        self.author = create_user("author@example.com")
        self.reader = create_user("reader@example.com")
        self.workspace = create_workspace(owner=self.author)
        add_workspace_member(workspace=self.workspace, user=self.reader)
        self.board = create_board(workspace=self.workspace, creator=self.author)
        add_board_member(board=self.board, user=self.reader)
        self.board_list = create_board_list(board=self.board)
        self.task = create_task(board_list=self.board_list, creator=self.author)
        self.client.force_authenticate(self.author)
        created = self.client.post(
            reverse("task-comment-create", args=[self.workspace.id, self.task.id]),
            {"body": "Looks good"},
            format="json",
        )
        self.url = reverse("task-comment-detail", args=[self.workspace.id, created.data["id"]])

    def test_a_collaborator_can_read_someone_else_s_comment(self):
        self.client.force_authenticate(self.reader)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["body"], "Looks good")

    def test_a_collaborator_still_cannot_edit_it(self):
        self.client.force_authenticate(self.reader)

        response = self.client.patch(self.url, {"body": "Edited"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_collaborator_still_cannot_delete_it(self):
        self.client.force_authenticate(self.reader)

        self.assertEqual(self.client.delete(self.url).status_code, 403)

    def test_a_stranger_gets_404_rather_than_403(self):
        stranger = create_user("stranger@example.com")
        self.client.force_authenticate(stranger)

        self.assertEqual(self.client.get(self.url).status_code, 404)


class TaskAndListLifecycleTests(APITestCase):
    """Archiving used to be unreachable: is_archived was filtered but never set."""

    def setUp(self):
        self.owner = create_user("owner@example.com")
        self.workspace = create_workspace(owner=self.owner)
        self.board = create_board(workspace=self.workspace, creator=self.owner)
        self.board_list = create_board_list(board=self.board)
        self.client.force_authenticate(self.owner)

    def test_a_task_can_be_archived(self):
        task = create_task(board_list=self.board_list, creator=self.owner)

        response = self.client.delete(reverse("task-detail", args=[self.workspace.id, task.id]))
        listing = self.client.get(
            reverse("task-list-create", args=[self.workspace.id, self.board_list.id])
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        task.refresh_from_db()
        self.assertTrue(task.is_archived)
        self.assertEqual(results(listing), [])

    def test_a_list_can_be_renamed_and_archived(self):
        url = reverse(
            "board-list-detail",
            args=[self.workspace.id, self.board.id, self.board_list.id],
        )

        rename = self.client.patch(url, {"name": "Renamed"}, format="json")
        archive = self.client.delete(url)
        listing = self.client.get(
            reverse("board-list-list-create", args=[self.workspace.id, self.board.id])
        )

        self.assertEqual(rename.status_code, status.HTTP_200_OK)
        self.assertEqual(rename.data["name"], "Renamed")
        self.assertEqual(archive.status_code, status.HTTP_204_NO_CONTENT)
        self.board_list.refresh_from_db()
        self.assertTrue(self.board_list.is_archived)
        self.assertEqual(results(listing), [])

    def test_a_viewer_cannot_archive_a_task(self):
        viewer = create_user("viewer@example.com")
        add_workspace_member(workspace=self.workspace, user=viewer)
        add_board_member(board=self.board, user=viewer, role=BoardMembership.Role.VIEWER)
        task = create_task(board_list=self.board_list, creator=self.owner)
        self.client.force_authenticate(viewer)

        response = self.client.delete(reverse("task-detail", args=[self.workspace.id, task.id]))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        task.refresh_from_db()
        self.assertFalse(task.is_archived)


class TaskDescriptionTests(APITestCase):
    def setUp(self):
        self.owner = create_user("owner@example.com")
        self.workspace = create_workspace(owner=self.owner)
        self.board = create_board(workspace=self.workspace, creator=self.owner)
        self.board_list = create_board_list(board=self.board)
        self.task = create_task(board_list=self.board_list, creator=self.owner)
        self.client.force_authenticate(self.owner)
        self.url = reverse("task-detail", args=[self.workspace.id, self.task.id])

    def test_an_oversized_description_is_rejected(self):
        huge = {"type": "doc", "text": "x" * 200_000}

        response = self.client.patch(self.url, {"description": huge}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("description", response.data)

    def test_a_description_change_is_logged_without_copying_the_document(self):
        document = {"type": "doc", "content": [{"type": "text", "text": "hello"}]}

        response = self.client.patch(self.url, {"description": document}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        activity = TaskActivity.objects.get(
            task=self.task,
            action=TaskActivity.Action.DESCRIPTION_CHANGED,
        )
        # The entry records that the description changed, not both copies.
        self.assertEqual(activity.payload, {})

    def test_a_title_change_still_records_both_values(self):
        response = self.client.patch(self.url, {"title": "Renamed"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        activity = TaskActivity.objects.get(
            task=self.task,
            action=TaskActivity.Action.TITLE_CHANGED,
        )
        self.assertEqual(activity.payload["to"], "Renamed")


class BoardMemberRemovalTests(APITestCase):
    def setUp(self):
        self.owner = create_user("owner@example.com")
        self.other_admin = create_user("admin@example.com")
        self.workspace = create_workspace(owner=self.owner)
        self.board = create_board(workspace=self.workspace, creator=self.owner)
        self.board_list = create_board_list(board=self.board)

    def test_removing_a_member_clears_their_mentions_in_one_statement(self):
        member = create_user("member@example.com")
        add_workspace_member(workspace=self.workspace, user=member)
        membership = add_board_member(board=self.board, user=member)
        task = create_task(board_list=self.board_list, creator=self.owner)
        TaskAssignment.objects.create(task=task, membership=membership)
        activity = TaskActivity.objects.create(
            task=task,
            author=self.owner,
            action=TaskActivity.Action.COMMENT_ADDED,
        )
        comment = TaskComment.objects.create(task=task, activity=activity, body="@member")
        comment.mentions.add(member)
        self.client.force_authenticate(self.owner)

        response = self.client.delete(
            reverse(
                "board-member-detail",
                args=[self.workspace.id, self.board.id, membership.id],
            )
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(comment.mentions.filter(pk=member.id).exists())
        self.assertFalse(TaskAssignment.objects.filter(task=task).exists())
