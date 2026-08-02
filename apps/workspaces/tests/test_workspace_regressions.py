"""Regression coverage for defects found in the workspace API review."""

from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.workspaces.models import Workspace, WorkspaceInvitation, WorkspaceMembership
from taskflow.testing import add_workspace_member, create_user, create_workspace


class WorkspaceMemberRemovalTests(APITestCase):
    """Workspace removal had none of the guards the board equivalent had."""

    def setUp(self):
        self.owner = create_user("owner@example.com")
        self.admin = create_user("admin@example.com")
        self.other_admin = create_user("other-admin@example.com")
        self.workspace = create_workspace(owner=self.owner)
        self.owner_membership = WorkspaceMembership.objects.get(
            workspace=self.workspace,
            user=self.owner,
        )
        self.admin_membership = add_workspace_member(
            workspace=self.workspace,
            user=self.admin,
            role=WorkspaceMembership.Role.ADMIN,
        )
        self.other_admin_membership = add_workspace_member(
            workspace=self.workspace,
            user=self.other_admin,
            role=WorkspaceMembership.Role.ADMIN,
        )

    def member_url(self, membership):
        return reverse("workspace-member-detail", args=[self.workspace.id, membership.id])

    def test_an_admin_cannot_remove_themselves(self):
        self.client.force_authenticate(self.admin)

        response = self.client.delete(self.member_url(self.admin_membership))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(WorkspaceMembership.objects.filter(pk=self.admin_membership.pk).exists())

    def test_the_owner_cannot_be_removed(self):
        self.client.force_authenticate(self.admin)

        response = self.client.delete(self.member_url(self.owner_membership))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(WorkspaceMembership.objects.filter(pk=self.owner_membership.pk).exists())

    def test_an_admin_can_remove_another_member(self):
        self.client.force_authenticate(self.admin)

        response = self.client.delete(self.member_url(self.other_admin_membership))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            WorkspaceMembership.objects.filter(pk=self.other_admin_membership.pk).exists()
        )

    def test_a_member_role_can_be_changed_but_not_to_owner_or_ones_own(self):
        self.client.force_authenticate(self.admin)

        promote = self.client.patch(
            self.member_url(self.other_admin_membership),
            {"role": WorkspaceMembership.Role.MEMBER},
            format="json",
        )
        own_role = self.client.patch(
            self.member_url(self.admin_membership),
            {"role": WorkspaceMembership.Role.MEMBER},
            format="json",
        )
        to_owner = self.client.patch(
            self.member_url(self.other_admin_membership),
            {"role": WorkspaceMembership.Role.OWNER},
            format="json",
        )

        self.assertEqual(promote.status_code, status.HTTP_200_OK)
        self.assertEqual(promote.data["role"], WorkspaceMembership.Role.MEMBER)
        self.assertEqual(own_role.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(to_owner.status_code, status.HTTP_400_BAD_REQUEST)


class WorkspaceDetailTests(APITestCase):
    """Workspaces previously had no retrieve, update, or delete endpoint."""

    def setUp(self):
        self.owner = create_user("owner@example.com")
        self.member = create_user("member@example.com")
        self.workspace = create_workspace(owner=self.owner, name="Product")
        add_workspace_member(workspace=self.workspace, user=self.member)
        self.url = reverse("workspace-detail", args=[self.workspace.id])

    def test_a_member_can_read_the_workspace(self):
        self.client.force_authenticate(self.member)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "Product")
        self.assertEqual(response.data["membership_role"], WorkspaceMembership.Role.MEMBER)

    def test_a_plain_member_cannot_rename_or_delete_it(self):
        self.client.force_authenticate(self.member)

        rename = self.client.patch(self.url, {"name": "Nope"}, format="json")
        delete = self.client.delete(self.url)

        self.assertEqual(rename.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(delete.status_code, status.HTTP_404_NOT_FOUND)

    def test_an_admin_can_rename_but_only_the_owner_can_delete(self):
        admin = create_user("admin@example.com")
        add_workspace_member(
            workspace=self.workspace,
            user=admin,
            role=WorkspaceMembership.Role.ADMIN,
        )
        self.client.force_authenticate(admin)

        rename = self.client.patch(self.url, {"name": "Renamed"}, format="json")
        delete = self.client.delete(self.url)

        self.assertEqual(rename.status_code, status.HTTP_200_OK)
        self.assertEqual(delete.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(Workspace.objects.filter(pk=self.workspace.pk).exists())

    def test_the_owner_can_delete_the_workspace(self):
        self.client.force_authenticate(self.owner)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Workspace.objects.filter(pk=self.workspace.pk).exists())


class WorkspaceInvitationExpiryTests(APITestCase):
    def setUp(self):
        self.owner = create_user("owner@example.com")
        self.invitee = create_user("invitee@example.com")
        self.workspace = create_workspace(owner=self.owner)
        self.invitation = WorkspaceInvitation.objects.create(
            workspace=self.workspace,
            email=self.invitee.email,
            invited_by=self.owner,
            invited_user=self.invitee,
        )

    def expire(self):
        self.invitation.expires_at = timezone.now() - timezone.timedelta(seconds=1)
        self.invitation.save(update_fields=["expires_at"])

    def test_an_expired_invitation_cannot_be_accepted(self):
        self.expire()
        self.client.force_authenticate(self.invitee)

        response = self.client.post(
            reverse("current-user-invitation-accept", args=[self.invitation.id])
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            WorkspaceMembership.objects.filter(
                workspace=self.workspace,
                user=self.invitee,
            ).exists()
        )

    def test_an_expired_link_is_not_publicly_readable(self):
        self.expire()

        response = self.client.get(
            reverse("public-workspace-invitation", args=[self.invitation.token])
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_an_expired_invitation_can_be_re_sent(self):
        self.expire()
        old_token = self.invitation.token
        self.client.force_authenticate(self.owner)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse(
                    "workspace-invitation-resend",
                    args=[self.workspace.id, self.invitation.id],
                )
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.invitation.refresh_from_db()
        self.assertNotEqual(self.invitation.token, old_token)
        self.assertTrue(self.invitation.is_actionable)

    def test_a_still_pending_invitation_cannot_be_re_sent(self):
        self.client.force_authenticate(self.owner)

        response = self.client.post(
            reverse(
                "workspace-invitation-resend",
                args=[self.workspace.id, self.invitation.id],
            )
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_registration_rejects_an_expired_invite_token(self):
        self.expire()

        response = self.client.post(
            reverse("accounts:register"),
            {
                "email": "brand-new@example.com",
                "display_name": "Brand new",
                "password": "strong-password-123",
                "invite_token": str(self.invitation.token),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("invite_token", response.data)
