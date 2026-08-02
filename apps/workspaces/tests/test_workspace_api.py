from django.core import mail
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.boards.models import Board, BoardMembership
from apps.tasks.models import BoardList, Task, TaskActivity, TaskAssignment, TaskComment
from apps.workspaces.models import Workspace, WorkspaceInvitation, WorkspaceMembership
from taskflow.testing import results


class WorkspaceApiTests(APITestCase):
    def setUp(self):
        mail.outbox.clear()
        self.user = User.objects.create_user(
            email="member@example.com",
            password="safe-password-123",
            display_name="Member",
        )
        self.other_user = User.objects.create_user(
            email="other@example.com",
            password="safe-password-123",
            display_name="Other",
        )
        self.url = reverse("workspace-list-create")

    def test_anonymous_user_cannot_list_or_create_workspaces(self):
        list_response = self.client.get(self.url)
        create_response = self.client.post(self.url, {"name": "Forbidden"})

        self.assertEqual(list_response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(create_response.status_code, status.HTTP_403_FORBIDDEN)

    def test_creating_a_workspace_also_creates_an_owner_membership(self):
        self.client.force_authenticate(self.user)

        response = self.client.post(
            self.url,
            {"name": "Product", "description": "Our product workspace"},
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        workspace = Workspace.objects.get(pk=response.data["id"])
        membership = WorkspaceMembership.objects.get(workspace=workspace, user=self.user)
        self.assertEqual(workspace.created_by, self.user)
        self.assertEqual(membership.role, WorkspaceMembership.Role.OWNER)
        self.assertEqual(response.data["membership_role"], "owner")

    def test_list_only_includes_the_current_users_workspaces(self):
        visible_workspace = Workspace.objects.create(name="Visible", created_by=self.user)
        WorkspaceMembership.objects.create(
            workspace=visible_workspace,
            user=self.user,
            role=WorkspaceMembership.Role.MEMBER,
        )
        hidden_workspace = Workspace.objects.create(name="Hidden", created_by=self.other_user)
        WorkspaceMembership.objects.create(
            workspace=hidden_workspace,
            user=self.other_user,
            role=WorkspaceMembership.Role.OWNER,
        )
        self.client.force_authenticate(self.user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(results(response)[0]["id"], visible_workspace.id)
        self.assertEqual(results(response)[0]["membership_role"], "member")
        self.assertEqual(len(results(response)), 1)

    def test_member_can_list_workspace_members(self):
        workspace = Workspace.objects.create(name="Product", created_by=self.user)
        WorkspaceMembership.objects.create(
            workspace=workspace,
            user=self.user,
            role=WorkspaceMembership.Role.OWNER,
        )
        WorkspaceMembership.objects.create(
            workspace=workspace,
            user=self.other_user,
            role=WorkspaceMembership.Role.MEMBER,
        )
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("workspace-member-list", args=[workspace.id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(results(response)), 2)
        self.assertEqual(results(response)[1]["display_name"], self.other_user.display_name)


class WorkspaceInvitationApiTests(APITestCase):
    def setUp(self):
        mail.outbox.clear()
        self.owner = User.objects.create_user(
            email="owner@example.com",
            password="safe-password-123",
            display_name="Owner",
        )
        self.invitee = User.objects.create_user(
            email="invitee@example.com",
            password="safe-password-123",
            display_name="Invitee",
        )
        self.workspace = Workspace.objects.create(name="Product", created_by=self.owner)
        self.owner_membership = WorkspaceMembership.objects.create(
            workspace=self.workspace,
            user=self.owner,
            role=WorkspaceMembership.Role.OWNER,
        )
        self.owner_url = reverse("workspace-invitation-list-create", args=[self.workspace.id])

    def invite(self, email="invitee@example.com"):
        self.client.force_authenticate(self.owner)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(self.owner_url, {"email": email}, format="json")

    def test_owner_can_invite_an_existing_user_and_mock_email_contains_link(self):
        response = self.invite()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        invitation = WorkspaceInvitation.objects.get(workspace=self.workspace)
        self.assertEqual(invitation.invited_user, self.invitee)
        self.assertEqual(invitation.status, WorkspaceInvitation.Status.PENDING)
        self.assertEqual(mail.outbox[0].to[0], self.invitee.email)
        self.assertIn(str(invitation.token), mail.outbox[0].body)

    def test_invite_rejects_existing_member_and_duplicate_pending_invite(self):
        WorkspaceMembership.objects.create(
            workspace=self.workspace,
            user=self.invitee,
            role=WorkspaceMembership.Role.MEMBER,
        )
        member_response = self.invite()

        self.assertEqual(member_response.status_code, status.HTTP_400_BAD_REQUEST)
        WorkspaceMembership.objects.filter(
            workspace=self.workspace,
            user=self.invitee,
        ).delete()
        self.assertEqual(self.invite().status_code, status.HTTP_201_CREATED)
        duplicate_response = self.invite(email="INVITEE@example.com")
        self.assertEqual(duplicate_response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_owner_cannot_manage_workspace_invitations(self):
        WorkspaceMembership.objects.create(
            workspace=self.workspace,
            user=self.invitee,
            role=WorkspaceMembership.Role.MEMBER,
        )
        self.client.force_authenticate(self.invitee)

        list_response = self.client.get(self.owner_url)
        create_response = self.client.post(
            self.owner_url,
            {"email": "new@example.com"},
            format="json",
        )

        self.assertEqual(list_response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(create_response.status_code, status.HTTP_404_NOT_FOUND)

    def test_recipient_can_accept_invitation_and_becomes_a_member(self):
        response = self.invite()
        self.client.force_authenticate(self.invitee)

        accept_response = self.client.post(
            reverse("current-user-invitation-accept", args=[response.data["id"]])
        )

        self.assertEqual(accept_response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertTrue(
            WorkspaceMembership.objects.filter(
                workspace=self.workspace,
                user=self.invitee,
                role=WorkspaceMembership.Role.MEMBER,
            ).exists()
        )
        self.assertEqual(
            WorkspaceInvitation.objects.get(pk=response.data["id"]).status,
            WorkspaceInvitation.Status.ACCEPTED,
        )

    def test_recipient_can_reject_and_owner_can_resend_with_a_new_token(self):
        response = self.invite()
        invitation = WorkspaceInvitation.objects.get(pk=response.data["id"])
        old_token = invitation.token
        self.client.force_authenticate(self.invitee)
        reject_response = self.client.post(
            reverse("current-user-invitation-reject", args=[invitation.id])
        )
        self.assertEqual(reject_response.status_code, status.HTTP_204_NO_CONTENT)

        self.client.force_authenticate(self.owner)
        with self.captureOnCommitCallbacks(execute=True):
            resend_response = self.client.post(
                reverse(
                    "workspace-invitation-resend",
                    args=[self.workspace.id, invitation.id],
                )
            )

        invitation.refresh_from_db()
        self.assertEqual(resend_response.status_code, status.HTTP_200_OK)
        self.assertEqual(invitation.status, WorkspaceInvitation.Status.PENDING)
        self.assertNotEqual(invitation.token, old_token)
        self.assertEqual(len(mail.outbox), 2)

    def test_unregistered_recipient_can_inspect_link_and_registration_attaches_invite(self):
        response = self.invite("new-user@example.com")
        invitation = WorkspaceInvitation.objects.get(pk=response.data["id"])
        self.client.force_authenticate(user=None)

        link_response = self.client.get(
            reverse("public-workspace-invitation", args=[invitation.token])
        )
        registration_response = self.client.post(
            reverse("accounts:register"),
            {
                "email": "new-user@example.com",
                "display_name": "New user",
                "password": "strong-password-123",
                "invite_token": str(invitation.token),
            },
            format="json",
        )

        self.assertEqual(link_response.status_code, status.HTTP_200_OK)
        self.assertFalse(link_response.data["recipient_registered"])
        self.assertEqual(registration_response.status_code, status.HTTP_201_CREATED)
        invitation.refresh_from_db()
        self.assertEqual(invitation.invited_user.email, "new-user@example.com")

    def test_registration_link_rejects_a_different_email(self):
        response = self.invite("new-user@example.com")
        invitation = WorkspaceInvitation.objects.get(pk=response.data["id"])
        self.client.force_authenticate(user=None)

        registration_response = self.client.post(
            reverse("accounts:register"),
            {
                "email": "wrong@example.com",
                "display_name": "Wrong user",
                "password": "strong-password-123",
                "invite_token": str(invitation.token),
            },
            format="json",
        )

        self.assertEqual(registration_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", registration_response.data)

    def test_removing_member_unassigns_tasks_clears_mentions_and_disables_profile(self):
        membership = WorkspaceMembership.objects.create(
            workspace=self.workspace,
            user=self.invitee,
            role=WorkspaceMembership.Role.MEMBER,
        )
        board = Board.objects.create(
            workspace=self.workspace,
            name="Roadmap",
            created_by=self.owner,
        )
        board_membership = BoardMembership.objects.create(
            board=board,
            user=self.invitee,
            role=BoardMembership.Role.MEMBER,
        )
        board_list = BoardList.objects.create(board=board, name="Todo")
        task = Task.objects.create(
            board_list=board_list,
            title="Ship invites",
            created_by=self.invitee,
        )
        TaskAssignment.objects.create(task=task, membership=board_membership)
        activity = TaskActivity.objects.create(
            task=task,
            author=self.owner,
            action=TaskActivity.Action.COMMENT_ADDED,
        )
        comment = TaskComment.objects.create(
            task=task,
            activity=activity,
            body="@Invitee please review",
        )
        comment.mentions.add(self.invitee)
        self.client.force_authenticate(self.owner)

        remove_response = self.client.delete(
            reverse("workspace-member-detail", args=[self.workspace.id, membership.id])
        )
        profile_response = self.client.get(
            reverse(
                "workspace-user-profile",
                args=[self.workspace.id, self.invitee.id],
            )
        )
        activity_response = self.client.get(
            reverse("task-activity-list", args=[self.workspace.id, task.id])
        )

        self.assertEqual(remove_response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(TaskAssignment.objects.filter(task=task).exists())
        self.assertFalse(comment.mentions.filter(pk=self.invitee.id).exists())
        self.assertEqual(profile_response.status_code, status.HTTP_404_NOT_FOUND)
        removed_author_event = TaskActivity.objects.create(
            task=task,
            author=self.invitee,
            action=TaskActivity.Action.TITLE_CHANGED,
        )
        activity_response = self.client.get(
            reverse("task-activity-list", args=[self.workspace.id, task.id])
        )
        event = next(
            item for item in results(activity_response) if item["id"] == removed_author_event.id
        )
        self.assertFalse(event["author_is_member"])
