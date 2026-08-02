"""Leaving, restoring, and the invitation race."""

from unittest.mock import patch

from django.db.models import QuerySet
from django.urls import reverse
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from apps.boards.models import BoardInvitation, BoardMembership
from apps.boards.services import create_board_invitation
from apps.tasks.models import TaskActivity, TaskComment
from apps.workspaces.models import WorkspaceMembership
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


class LeaveBoardTests(APITestCase):
    def setUp(self):
        self.owner = create_user("owner@example.com")
        self.member = create_user("member@example.com")
        self.workspace = create_workspace(owner=self.owner)
        add_workspace_member(workspace=self.workspace, user=self.member)
        self.board = create_board(workspace=self.workspace, creator=self.owner)
        self.membership = add_board_member(board=self.board, user=self.member)
        self.url = reverse("board-leave", args=[self.workspace.id, self.board.id])

    def test_a_member_can_leave(self):
        self.client.force_authenticate(self.member)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            BoardMembership.objects.filter(board=self.board, user=self.member).exists()
        )

    def test_leaving_clears_the_departing_member_s_mentions(self):
        board_list = create_board_list(board=self.board)
        task = create_task(board_list=board_list, creator=self.owner)
        activity = TaskActivity.objects.create(
            task=task, author=self.owner, action=TaskActivity.Action.COMMENT_ADDED
        )
        comment = TaskComment.objects.create(task=task, activity=activity, body="@member")
        comment.mentions.add(self.member)
        self.client.force_authenticate(self.member)

        self.client.delete(self.url)

        self.assertFalse(comment.mentions.filter(pk=self.member.id).exists())

    def test_the_last_admin_cannot_leave(self):
        # Workspace ownership does not substitute for a board admin.
        self.client.force_authenticate(self.owner)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(BoardMembership.objects.filter(board=self.board, user=self.owner).exists())

    def test_an_admin_can_leave_once_another_admin_exists(self):
        second_admin = create_user("admin2@example.com")
        add_workspace_member(workspace=self.workspace, user=second_admin)
        add_board_member(board=self.board, user=second_admin, role=BoardMembership.Role.ADMIN)
        self.client.force_authenticate(self.owner)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_a_non_member_gets_404(self):
        stranger = create_user("stranger@example.com")
        self.client.force_authenticate(stranger)

        self.assertEqual(self.client.delete(self.url).status_code, 404)


class LeaveWorkspaceTests(APITestCase):
    def setUp(self):
        self.owner = create_user("owner@example.com")
        self.member = create_user("member@example.com")
        self.workspace = create_workspace(owner=self.owner)
        add_workspace_member(workspace=self.workspace, user=self.member)
        self.board = create_board(workspace=self.workspace, creator=self.owner)
        add_board_member(board=self.board, user=self.member)
        self.url = reverse("workspace-leave", args=[self.workspace.id])

    def test_a_member_can_leave_and_loses_their_board_memberships(self):
        self.client.force_authenticate(self.member)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            WorkspaceMembership.objects.filter(workspace=self.workspace, user=self.member).exists()
        )
        self.assertFalse(
            BoardMembership.objects.filter(board=self.board, user=self.member).exists()
        )

    def test_the_owner_cannot_leave(self):
        self.client.force_authenticate(self.owner)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(
            WorkspaceMembership.objects.filter(workspace=self.workspace, user=self.owner).exists()
        )

    def test_removing_someone_else_still_refuses_self_removal(self):
        """Leaving is deliberate; removing yourself from the admin list is not."""
        admin = create_user("admin@example.com")
        membership = add_workspace_member(
            workspace=self.workspace, user=admin, role=WorkspaceMembership.Role.ADMIN
        )
        self.client.force_authenticate(admin)

        response = self.client.delete(
            reverse("workspace-member-detail", args=[self.workspace.id, membership.id])
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class BoardArchiveRoundTripTests(APITestCase):
    def setUp(self):
        self.owner = create_user("owner@example.com")
        self.workspace = create_workspace(owner=self.owner)
        self.board = create_board(workspace=self.workspace, creator=self.owner)
        self.client.force_authenticate(self.owner)
        self.client.delete(reverse("board-detail", args=[self.workspace.id, self.board.id]))

    def test_an_archived_board_is_hidden_from_the_default_listing(self):
        self.assertEqual(results(self.client.get(reverse("my-board-list"))), [])

    def test_an_archived_board_is_listed_on_request(self):
        response = self.client.get(reverse("my-board-list"), {"archived": "true"})

        self.assertEqual(len(results(response)), 1)
        self.assertTrue(results(response)[0]["is_archived"])

    def test_an_archived_board_can_be_restored(self):
        response = self.client.post(
            reverse("board-restore", args=[self.workspace.id, self.board.id])
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.board.refresh_from_db()
        self.assertFalse(self.board.is_archived)
        self.assertEqual(len(results(self.client.get(reverse("my-board-list")))), 1)

    def test_restoring_an_active_board_is_a_404(self):
        self.client.post(reverse("board-restore", args=[self.workspace.id, self.board.id]))

        response = self.client.post(
            reverse("board-restore", args=[self.workspace.id, self.board.id])
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_plain_member_cannot_restore(self):
        member = create_user("member@example.com")
        add_workspace_member(workspace=self.workspace, user=member)
        self.client.force_authenticate(member)

        response = self.client.post(
            reverse("board-restore", args=[self.workspace.id, self.board.id])
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class InvitationRaceTests(APITestCase):
    """`select_for_update()` cannot lock a row that does not exist yet."""

    def setUp(self):
        self.owner = create_user("owner@example.com")
        self.workspace = create_workspace(owner=self.owner)
        self.board = create_board(workspace=self.workspace, creator=self.owner)

    def test_the_losing_side_of_a_concurrent_invite_gets_a_validation_error(self):
        BoardInvitation.objects.create(
            board=self.board, email="invitee@example.com", invited_by=self.owner
        )
        real_first = QuerySet.first

        def blind_to_invitations(queryset):
            # Stands in for the other transaction committing after this one
            # checked. Only invitation lookups are blinded.
            if queryset.model is BoardInvitation:
                return None
            return real_first(queryset)

        with patch.object(QuerySet, "first", blind_to_invitations):
            with self.assertRaises(ValidationError) as raised:
                create_board_invitation(
                    board=self.board,
                    email="invitee@example.com",
                    role=BoardMembership.Role.MEMBER,
                    invited_by=self.owner,
                )

        # Without the savepoint and the IntegrityError guard this was a 500.
        self.assertIn("email", raised.exception.detail)
        self.assertEqual(BoardInvitation.objects.count(), 1)
