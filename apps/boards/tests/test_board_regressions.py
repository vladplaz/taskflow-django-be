"""Regression coverage for defects found in the board API review."""

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.boards.models import Board, BoardInvitation, BoardMembership
from apps.boards.services import create_board
from taskflow.invitations import default_invitation_expiry
from taskflow.testing import add_workspace_member, create_user, create_workspace, results


class BoardScopedNameTests(APITestCase):
    """A duplicate name used to reach the database and surface as a 500."""

    def setUp(self):
        self.owner = create_user("owner@example.com")
        self.workspace = create_workspace(owner=self.owner)
        self.board = create_board(
            workspace=self.workspace,
            creator=self.owner,
            name="Website",
            description="",
        )
        self.client.force_authenticate(self.owner)

    def test_duplicate_status_name_is_rejected_with_400(self):
        url = reverse("board-status-list-create", args=[self.workspace.id, self.board.id])

        response = self.client.post(url, {"name": "To do"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("name", response.data)

    def test_duplicate_label_name_is_rejected_with_400(self):
        url = reverse("board-label-list-create", args=[self.workspace.id, self.board.id])

        response = self.client.post(url, {"name": "Bug", "color": "#111111"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("name", response.data)

    def test_a_status_can_keep_its_own_name_when_updated(self):
        url = reverse("board-status-list-create", args=[self.workspace.id, self.board.id])
        created = self.client.post(url, {"name": "Blocked"}, format="json")
        detail_url = reverse(
            "board-status-detail",
            args=[self.workspace.id, self.board.id, created.data["id"]],
        )

        response = self.client.patch(detail_url, {"name": "Blocked"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)


class BoardArchiveTests(APITestCase):
    def setUp(self):
        self.owner = create_user("owner@example.com")
        self.workspace = create_workspace(owner=self.owner)
        self.board = create_board(
            workspace=self.workspace,
            creator=self.owner,
            name="Website",
            description="",
        )
        self.client.force_authenticate(self.owner)

    def test_delete_archives_the_board_and_hides_it_from_listings(self):
        url = reverse("board-detail", args=[self.workspace.id, self.board.id])

        response = self.client.delete(url)
        listing = self.client.get(reverse("my-board-list"))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.board.refresh_from_db()
        self.assertTrue(self.board.is_archived)
        self.assertEqual(results(listing), [])

    def test_a_non_admin_cannot_archive_the_board(self):
        member = create_user("member@example.com")
        add_workspace_member(workspace=self.workspace, user=member)
        BoardMembership.objects.create(
            board=self.board,
            user=member,
            role=BoardMembership.Role.MEMBER,
        )
        self.client.force_authenticate(member)

        response = self.client.delete(
            reverse("board-detail", args=[self.workspace.id, self.board.id])
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.board.refresh_from_db()
        self.assertFalse(self.board.is_archived)


class BoardInvitationLinkTests(APITestCase):
    """An emailed token must stop disclosing the invitee once it is spent."""

    def setUp(self):
        self.owner = create_user("owner@example.com")
        self.workspace = create_workspace(owner=self.owner)
        self.board = create_board(
            workspace=self.workspace,
            creator=self.owner,
            name="Website",
            description="",
        )
        self.invitation = BoardInvitation.objects.create(
            board=self.board,
            email="guest@example.com",
            invited_by=self.owner,
        )

    def public_url(self):
        return reverse("public-board-invitation", args=[self.invitation.token])

    def test_a_pending_invitation_is_readable(self):
        response = self.client.get(self.public_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["email"], "guest@example.com")

    def test_an_accepted_invitation_no_longer_exposes_the_invitee(self):
        self.invitation.status = BoardInvitation.Status.ACCEPTED
        self.invitation.save(update_fields=["status"])

        response = self.client.get(self.public_url())

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_an_expired_invitation_no_longer_exposes_the_invitee(self):
        self.invitation.expires_at = timezone.now() - timezone.timedelta(seconds=1)
        self.invitation.save(update_fields=["expires_at"])

        response = self.client.get(self.public_url())

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_an_expired_invitation_cannot_be_accepted(self):
        guest = create_user("guest@example.com")
        self.invitation.expires_at = timezone.now() - timezone.timedelta(seconds=1)
        self.invitation.save(update_fields=["expires_at"])
        self.client.force_authenticate(guest)

        response = self.client.post(
            reverse("current-user-board-invitation-accept", args=[self.invitation.id])
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(BoardMembership.objects.filter(board=self.board, user=guest).exists())

    def test_an_expired_invitation_can_be_resent_with_a_fresh_deadline(self):
        self.invitation.expires_at = timezone.now() - timezone.timedelta(seconds=1)
        self.invitation.save(update_fields=["expires_at"])
        old_token = self.invitation.token
        self.client.force_authenticate(self.owner)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse(
                    "board-invitation-resend",
                    args=[self.workspace.id, self.board.id, self.invitation.id],
                )
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.invitation.refresh_from_db()
        self.assertNotEqual(self.invitation.token, old_token)
        self.assertGreater(self.invitation.expires_at, timezone.now())

    def test_new_invitations_expire_in_the_future(self):
        self.assertGreater(default_invitation_expiry(), timezone.now())


class BoardQueryCountTests(APITestCase):
    """The board listing used to cost three extra queries per board."""

    def setUp(self):
        self.owner = create_user("owner@example.com")
        self.workspace = create_workspace(owner=self.owner)
        self.client.force_authenticate(self.owner)
        self.url = reverse("my-board-list")

    def add_boards(self, count: int, prefix: str):
        for index in range(count):
            create_board(
                workspace=self.workspace,
                creator=self.owner,
                name=f"{prefix}-{index}",
                description="",
            )

    def test_listing_boards_costs_a_constant_number_of_queries(self):
        self.add_boards(2, "small")
        with CaptureQueriesContext(connection) as few:
            small_response = self.client.get(self.url)

        self.add_boards(10, "large")
        with CaptureQueriesContext(connection) as many:
            large_response = self.client.get(self.url)

        self.assertEqual(len(results(small_response)), 2)
        self.assertEqual(len(results(large_response)), 12)
        self.assertEqual(len(few.captured_queries), len(many.captured_queries))

    def test_listing_board_members_costs_a_constant_number_of_queries(self):
        board = create_board(
            workspace=self.workspace,
            creator=self.owner,
            name="Members",
            description="",
        )
        url = reverse("board-member-list", args=[self.workspace.id, board.id])
        with CaptureQueriesContext(connection) as few:
            self.client.get(url)

        for index in range(8):
            member = create_user(f"member-{index}@example.com")
            BoardMembership.objects.create(
                board=board,
                user=member,
                role=BoardMembership.Role.MEMBER,
            )
        with CaptureQueriesContext(connection) as many:
            response = self.client.get(url)

        self.assertEqual(len(results(response)), 9)
        self.assertEqual(len(few.captured_queries), len(many.captured_queries))

    def test_access_role_is_still_correct_when_annotated(self):
        board = create_board(
            workspace=self.workspace,
            creator=self.owner,
            name="Roles",
            description="",
            visibility=Board.Visibility.WORKSPACE,
        )
        viewer = create_user("viewer@example.com")
        add_workspace_member(workspace=self.workspace, user=viewer)
        self.client.force_authenticate(viewer)

        response = self.client.get(reverse("my-board-list"))

        row = next(item for item in results(response) if item["id"] == board.id)
        self.assertEqual(row["access_role"], BoardMembership.Role.VIEWER)
        self.assertFalse(row["can_edit"])
        self.assertFalse(row["can_admin"])


class PaginationTests(APITestCase):
    def setUp(self):
        self.owner = create_user("owner@example.com")
        self.workspace = create_workspace(owner=self.owner)
        self.client.force_authenticate(self.owner)

    def test_list_responses_are_paginated(self):
        for index in range(3):
            create_board(
                workspace=self.workspace,
                creator=self.owner,
                name=f"Board {index}",
                description="",
            )

        response = self.client.get(reverse("my-board-list"), {"page_size": 2})

        self.assertEqual(response.data["count"], 3)
        self.assertEqual(len(response.data["results"]), 2)
        self.assertIsNotNone(response.data["next"])

    def test_page_size_is_capped(self):
        response = self.client.get(reverse("my-board-list"), {"page_size": 10_000})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
