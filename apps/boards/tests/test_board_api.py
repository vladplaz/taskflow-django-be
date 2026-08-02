from django.core import mail
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.boards.models import (
    Board,
    BoardInvitation,
    BoardLabel,
    BoardMembership,
    BoardStatus,
)
from apps.tasks.models import BoardList, Task, TaskAssignment
from apps.workspaces.models import Workspace, WorkspaceMembership
from taskflow.testing import results


class BoardApiTests(APITestCase):
    def setUp(self):
        self.member = User.objects.create_user(
            email="member@example.com", password="safe-password-123"
        )
        self.outsider = User.objects.create_user(
            email="outsider@example.com", password="safe-password-123"
        )
        self.workspace = Workspace.objects.create(name="Product", created_by=self.member)
        WorkspaceMembership.objects.create(
            workspace=self.workspace,
            user=self.member,
            role=WorkspaceMembership.Role.OWNER,
        )
        self.url = reverse("workspace-board-list-create", args=[self.workspace.id])

    def test_member_can_create_a_board_in_their_workspace(self):
        self.client.force_authenticate(self.member)

        response = self.client.post(
            self.url,
            {"name": "Website", "description": "The marketing site"},
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        board = Board.objects.get(pk=response.data["id"])
        self.assertEqual(board.workspace, self.workspace)
        self.assertEqual(board.created_by, self.member)
        self.assertTrue(
            BoardMembership.objects.filter(
                board=board,
                user=self.member,
                role=BoardMembership.Role.ADMIN,
            ).exists()
        )
        self.assertEqual(
            list(BoardStatus.objects.filter(board=board).values_list("name", flat=True)),
            ["To do", "In progress", "In review", "Done"],
        )
        self.assertEqual(
            list(BoardLabel.objects.filter(board=board).values_list("name", "color")),
            [("Feature", "#2563EB"), ("Bug", "#DC2626"), ("Improvement", "#16A34A")],
        )

    def test_member_can_manage_board_statuses(self):
        board = Board.objects.create(
            name="Website", workspace=self.workspace, created_by=self.member
        )
        list_url = reverse("board-status-list-create", args=[self.workspace.id, board.id])
        self.client.force_authenticate(self.member)

        create_response = self.client.post(list_url, {"name": "Blocked"})

        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        detail_url = reverse(
            "board-status-detail",
            args=[self.workspace.id, board.id, create_response.data["id"]],
        )
        update_response = self.client.patch(detail_url, {"name": "Waiting"})
        delete_response = self.client.delete(detail_url)

        self.assertEqual(update_response.status_code, status.HTTP_200_OK)
        self.assertEqual(update_response.data["name"], "Waiting")
        self.assertEqual(delete_response.status_code, status.HTTP_204_NO_CONTENT)

    def test_member_can_manage_colored_board_labels(self):
        board = Board.objects.create(
            name="Website", workspace=self.workspace, created_by=self.member
        )
        list_url = reverse("board-label-list-create", args=[self.workspace.id, board.id])
        self.client.force_authenticate(self.member)

        create_response = self.client.post(list_url, {"name": "Urgent", "color": "#F97316"})

        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(create_response.data["color"], "#F97316")
        detail_url = reverse(
            "board-label-detail",
            args=[self.workspace.id, board.id, create_response.data["id"]],
        )
        update_response = self.client.patch(
            detail_url, {"name": "High priority", "color": "#EA580C"}
        )
        delete_response = self.client.delete(detail_url)

        self.assertEqual(update_response.status_code, status.HTTP_200_OK)
        self.assertEqual(update_response.data["name"], "High priority")
        self.assertEqual(update_response.data["color"], "#EA580C")
        self.assertEqual(delete_response.status_code, status.HTTP_204_NO_CONTENT)

    def test_member_can_rename_a_board(self):
        board = Board.objects.create(
            name="Website", workspace=self.workspace, created_by=self.member
        )
        detail_url = reverse("board-detail", args=[self.workspace.id, board.id])
        self.client.force_authenticate(self.member)

        response = self.client.patch(detail_url, {"name": "Marketing website"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        board.refresh_from_db()
        self.assertEqual(board.name, "Marketing website")

    def test_non_member_cannot_see_a_workspace_boards(self):
        Board.objects.create(name="Private", workspace=self.workspace, created_by=self.member)
        self.client.force_authenticate(self.outsider)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(results(response), [])


class BoardAccessAndInvitationApiTests(APITestCase):
    def setUp(self):
        mail.outbox.clear()
        self.owner = User.objects.create_user(
            email="owner@example.com",
            password="safe-password-123",
            display_name="Owner",
        )
        self.workspace_member = User.objects.create_user(
            email="workspace-member@example.com",
            password="safe-password-123",
            display_name="Workspace member",
        )
        self.guest = User.objects.create_user(
            email="guest@example.com",
            password="safe-password-123",
            display_name="Guest",
        )
        self.workspace = Workspace.objects.create(name="Product", created_by=self.owner)
        WorkspaceMembership.objects.create(
            workspace=self.workspace,
            user=self.owner,
            role=WorkspaceMembership.Role.OWNER,
        )
        WorkspaceMembership.objects.create(
            workspace=self.workspace,
            user=self.workspace_member,
            role=WorkspaceMembership.Role.MEMBER,
        )
        self.private_board = Board.objects.create(
            workspace=self.workspace,
            name="Private roadmap",
            created_by=self.owner,
            visibility=Board.Visibility.PRIVATE,
        )
        self.workspace_board = Board.objects.create(
            workspace=self.workspace,
            name="Company roadmap",
            created_by=self.owner,
            visibility=Board.Visibility.WORKSPACE,
        )
        self.invitation_url = reverse(
            "board-invitation-list-create",
            args=[self.workspace.id, self.private_board.id],
        )

    def invite(self, email="guest@example.com", role=BoardMembership.Role.MEMBER):
        self.client.force_authenticate(self.owner)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                self.invitation_url,
                {"email": email, "role": role},
                format="json",
            )

    def test_workspace_member_only_discovers_workspace_visible_boards_as_viewer(self):
        self.client.force_authenticate(self.workspace_member)

        response = self.client.get(reverse("workspace-board-list-create", args=[self.workspace.id]))
        private_detail = self.client.get(
            reverse(
                "board-detail",
                args=[self.workspace.id, self.private_board.id],
            )
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [board["id"] for board in results(response)],
            [self.workspace_board.id],
        )
        self.assertEqual(results(response)[0]["access_role"], BoardMembership.Role.VIEWER)
        self.assertFalse(results(response)[0]["can_edit"])
        self.assertEqual(private_detail.status_code, status.HTTP_404_NOT_FOUND)

    def test_regular_workspace_member_cannot_create_a_board(self):
        self.client.force_authenticate(self.workspace_member)

        response = self.client.post(
            reverse("workspace-board-list-create", args=[self.workspace.id]),
            {"name": "Unauthorized", "visibility": Board.Visibility.PRIVATE},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_board_invitation_acceptance_adds_a_board_guest_without_workspace_membership(self):
        response = self.invite(role=BoardMembership.Role.VIEWER)
        invitation = BoardInvitation.objects.get(pk=response.data["id"])

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(mail.outbox[0].to[0], self.guest.email)
        self.assertIn(str(invitation.token), mail.outbox[0].body)

        self.client.force_authenticate(self.guest)
        accept_response = self.client.post(
            reverse("current-user-board-invitation-accept", args=[invitation.id])
        )
        boards_response = self.client.get(reverse("my-board-list"))

        self.assertEqual(accept_response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            WorkspaceMembership.objects.filter(
                workspace=self.workspace,
                user=self.guest,
            ).exists()
        )
        membership = BoardMembership.objects.get(
            board=self.private_board,
            user=self.guest,
        )
        self.assertEqual(membership.role, BoardMembership.Role.VIEWER)
        self.assertEqual(results(boards_response)[0]["id"], self.private_board.id)
        self.assertFalse(results(boards_response)[0]["can_edit"])

    def test_rejected_board_invitation_can_be_resent_with_a_new_token(self):
        response = self.invite()
        invitation = BoardInvitation.objects.get(pk=response.data["id"])
        old_token = invitation.token
        self.client.force_authenticate(self.guest)
        reject_response = self.client.post(
            reverse("current-user-board-invitation-reject", args=[invitation.id])
        )
        self.client.force_authenticate(self.owner)
        with self.captureOnCommitCallbacks(execute=True):
            resend_response = self.client.post(
                reverse(
                    "board-invitation-resend",
                    args=[self.workspace.id, self.private_board.id, invitation.id],
                )
            )

        invitation.refresh_from_db()
        self.assertEqual(reject_response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(resend_response.status_code, status.HTTP_200_OK)
        self.assertEqual(invitation.status, BoardInvitation.Status.PENDING)
        self.assertNotEqual(invitation.token, old_token)
        self.assertEqual(len(mail.outbox), 2)

    def test_unregistered_board_invitee_must_register_with_the_invited_email(self):
        response = self.invite(email="new-user@example.com")
        invitation = BoardInvitation.objects.get(pk=response.data["id"])
        self.client.force_authenticate(user=None)

        wrong_email_response = self.client.post(
            reverse("accounts:register"),
            {
                "email": "wrong@example.com",
                "display_name": "Wrong",
                "password": "strong-password-123",
                "board_invite_token": str(invitation.token),
            },
            format="json",
        )
        registration_response = self.client.post(
            reverse("accounts:register"),
            {
                "email": "new-user@example.com",
                "display_name": "New user",
                "password": "strong-password-123",
                "board_invite_token": str(invitation.token),
            },
            format="json",
        )

        self.assertEqual(wrong_email_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", wrong_email_response.data)
        self.assertEqual(registration_response.status_code, status.HTTP_201_CREATED)
        invitation.refresh_from_db()
        self.assertEqual(invitation.invited_user.email, "new-user@example.com")

    def test_removing_a_board_member_unassigns_their_tasks(self):
        membership = BoardMembership.objects.create(
            board=self.private_board,
            user=self.guest,
            role=BoardMembership.Role.MEMBER,
        )
        owner_membership = BoardMembership.objects.create(
            board=self.private_board,
            user=self.owner,
            role=BoardMembership.Role.ADMIN,
        )
        board_list = BoardList.objects.create(board=self.private_board, name="Todo")
        task = Task.objects.create(
            board_list=board_list,
            title="Guest task",
            created_by=self.owner,
        )
        TaskAssignment.objects.create(task=task, membership=membership)
        self.client.force_authenticate(self.owner)

        response = self.client.delete(
            reverse(
                "board-member-detail",
                args=[self.workspace.id, self.private_board.id, membership.id],
            )
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(TaskAssignment.objects.filter(task=task).exists())
        self.assertTrue(BoardMembership.objects.filter(pk=owner_membership.id).exists())

    def test_board_admin_cannot_change_or_remove_their_own_membership(self):
        owner_membership = BoardMembership.objects.create(
            board=self.private_board,
            user=self.owner,
            role=BoardMembership.Role.ADMIN,
        )
        BoardMembership.objects.create(
            board=self.private_board,
            user=self.guest,
            role=BoardMembership.Role.ADMIN,
        )
        membership_url = reverse(
            "board-member-detail",
            args=[self.workspace.id, self.private_board.id, owner_membership.id],
        )
        self.client.force_authenticate(self.owner)

        update_response = self.client.patch(
            membership_url,
            {"role": BoardMembership.Role.MEMBER},
            format="json",
        )
        remove_response = self.client.delete(membership_url)

        self.assertEqual(update_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            update_response.data["detail"],
            "You cannot change your own board role",
        )
        self.assertEqual(remove_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            remove_response.data["detail"],
            "You cannot remove yourself from this board",
        )
        owner_membership.refresh_from_db()
        self.assertEqual(owner_membership.role, BoardMembership.Role.ADMIN)
