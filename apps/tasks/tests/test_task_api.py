from decimal import Decimal

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.boards.models import Board, BoardLabel, BoardMembership, BoardStatus
from apps.tasks.models import BoardList, Task, TaskAssignment
from apps.workspaces.models import Workspace, WorkspaceMembership
from taskflow.testing import results


class TaskApiTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            email="owner@example.com", password="safe-password-123"
        )
        self.member = User.objects.create_user(
            email="member@example.com", password="safe-password-123"
        )
        self.outsider = User.objects.create_user(
            email="outsider@example.com", password="safe-password-123"
        )
        self.workspace = Workspace.objects.create(name="Product", created_by=self.owner)
        self.owner_membership = WorkspaceMembership.objects.create(
            workspace=self.workspace,
            user=self.owner,
            role=WorkspaceMembership.Role.OWNER,
        )
        self.member_membership = WorkspaceMembership.objects.create(
            workspace=self.workspace,
            user=self.member,
            role=WorkspaceMembership.Role.MEMBER,
        )
        other_workspace = Workspace.objects.create(name="Other", created_by=self.outsider)
        self.outsider_membership = WorkspaceMembership.objects.create(
            workspace=other_workspace,
            user=self.outsider,
            role=WorkspaceMembership.Role.OWNER,
        )
        self.board = Board.objects.create(
            name="Website", workspace=self.workspace, created_by=self.owner
        )
        self.owner_board_membership = BoardMembership.objects.create(
            board=self.board,
            user=self.owner,
            role=BoardMembership.Role.ADMIN,
        )
        self.member_board_membership = BoardMembership.objects.create(
            board=self.board,
            user=self.member,
            role=BoardMembership.Role.MEMBER,
        )
        outsider_board = Board.objects.create(
            name="Other board",
            workspace=other_workspace,
            created_by=self.outsider,
        )
        self.outsider_board_membership = BoardMembership.objects.create(
            board=outsider_board,
            user=self.outsider,
            role=BoardMembership.Role.ADMIN,
        )
        self.board_status = BoardStatus.objects.create(
            name="To do", board=self.board, position=1000
        )
        self.feature_label = BoardLabel.objects.create(
            name="Feature", color="#2563EB", board=self.board, position=1000
        )
        self.bug_label = BoardLabel.objects.create(
            name="Bug", color="#DC2626", board=self.board, position=2000
        )
        self.board_list = BoardList.objects.create(name="To do", board=self.board)
        self.list_url = reverse(
            "board-list-list-create",
            args=[self.workspace.id, self.board.id],
        )
        self.task_url = reverse(
            "task-list-create",
            args=[self.workspace.id, self.board_list.id],
        )

    def test_member_can_create_a_board_list(self):
        self.client.force_authenticate(self.member)

        response = self.client.post(self.list_url, {"name": "In progress"})

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["position"], "2000.0000000000")

    def test_task_can_have_multiple_assignees_from_the_same_workspace(self):
        self.client.force_authenticate(self.owner)

        response = self.client.post(
            self.task_url,
            {
                "title": "Ship workspace API",
                "assignee_membership_ids": [
                    self.owner_board_membership.id,
                    self.member_board_membership.id,
                ],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        task = Task.objects.get(pk=response.data["id"])
        self.assertEqual(TaskAssignment.objects.filter(task=task).count(), 2)
        self.assertEqual(
            {assignee["user_id"] for assignee in response.data["assignees"]},
            {self.owner.id, self.member.id},
        )

    def test_task_priority_can_be_created_updated_and_cleared(self):
        self.client.force_authenticate(self.owner)

        create_response = self.client.post(
            self.task_url,
            {"title": "Ship priorities", "priority": Task.Priority.HIGH},
            format="json",
        )

        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(create_response.data["priority"], Task.Priority.HIGH)
        task = Task.objects.get(pk=create_response.data["id"])
        self.assertEqual(task.priority, Task.Priority.HIGH)

        detail_url = reverse("task-detail", args=[self.workspace.id, task.id])
        clear_response = self.client.patch(detail_url, {"priority": None}, format="json")

        self.assertEqual(clear_response.status_code, status.HTTP_200_OK)
        self.assertIsNone(clear_response.data["priority"])

    def test_task_activity_records_creation_and_changes(self):
        self.client.force_authenticate(self.owner)
        create_response = self.client.post(
            self.task_url, {"title": "Original title"}, format="json"
        )
        task_id = create_response.data["id"]

        detail_url = reverse("task-detail", args=[self.workspace.id, task_id])
        self.client.patch(detail_url, {"title": "Renamed task"}, format="json")
        activity_url = reverse("task-activity-list", args=[self.workspace.id, task_id])
        activity_response = self.client.get(activity_url)

        self.assertEqual(activity_response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [entry["action"] for entry in results(activity_response)],
            ["title_changed", "created"],
        )
        self.assertEqual(results(activity_response)[0]["payload"]["from"], "Original title")
        self.assertEqual(results(activity_response)[0]["author_id"], self.owner.id)

    def test_activity_is_read_in_batches_that_need_not_line_up_with_pages(self):
        """The card opens with a short batch and asks for larger ones after it."""
        task = Task.objects.create(
            title="Long lived", board_list=self.board_list, created_by=self.owner
        )
        self.client.force_authenticate(self.owner)
        comment_url = reverse("task-comment-create", args=[self.workspace.id, task.id])
        for index in range(12):
            self.client.post(comment_url, {"body": f"Comment {index}"}, format="json")

        activity_url = reverse("task-activity-list", args=[self.workspace.id, task.id])
        first = self.client.get(activity_url, {"limit": 5, "offset": 0})
        second = self.client.get(activity_url, {"limit": 10, "offset": 5})

        self.assertEqual(first.data["count"], 12)
        self.assertEqual(len(results(first)), 5)
        self.assertIsNotNone(first.data["next"])
        # The remaining seven, starting exactly where the first batch stopped.
        self.assertEqual(len(results(second)), 7)
        self.assertIsNone(second.data["next"])
        overlap = {entry["id"] for entry in results(first)} & {
            entry["id"] for entry in results(second)
        }
        self.assertEqual(overlap, set())

    def test_comment_is_a_timeline_entry_and_only_its_author_can_change_it(self):
        task = Task.objects.create(
            title="Discuss", board_list=self.board_list, created_by=self.owner
        )
        self.client.force_authenticate(self.owner)
        comment_url = reverse("task-comment-create", args=[self.workspace.id, task.id])
        create_response = self.client.post(comment_url, {"body": "Looks good."}, format="json")

        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        activity_url = reverse("task-activity-list", args=[self.workspace.id, task.id])
        activity_response = self.client.get(activity_url)
        self.assertEqual(results(activity_response)[0]["comment_body"], "Looks good.")

        detail_url = reverse(
            "task-comment-detail", args=[self.workspace.id, create_response.data["id"]]
        )
        self.client.force_authenticate(self.member)
        self.assertEqual(
            self.client.patch(detail_url, {"body": "Changed"}, format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_comment_rejects_mentions_outside_the_workspace(self):
        task = Task.objects.create(
            title="Discuss", board_list=self.board_list, created_by=self.owner
        )
        self.client.force_authenticate(self.owner)

        response = self.client.post(
            reverse("task-comment-create", args=[self.workspace.id, task.id]),
            {"body": "Private note", "mention_user_ids": [self.outsider.id]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(task.comments.exists())

    def test_task_status_can_be_set_replaced_or_cleared(self):
        self.client.force_authenticate(self.owner)
        create_response = self.client.post(
            self.task_url,
            {"title": "Ship statuses", "status_id": self.board_status.id},
            format="json",
        )

        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        task = Task.objects.get(pk=create_response.data["id"])
        self.assertEqual(task.status, self.board_status)
        self.assertEqual(create_response.data["status"]["name"], "To do")

        detail_url = reverse("task-detail", args=[self.workspace.id, task.id])
        clear_response = self.client.patch(detail_url, {"status_id": None}, format="json")

        self.assertEqual(clear_response.status_code, status.HTTP_200_OK)
        task.refresh_from_db()
        self.assertIsNone(task.status)

    def test_task_rejects_a_status_from_another_board(self):
        another_board = Board.objects.create(
            name="Mobile", workspace=self.workspace, created_by=self.owner
        )
        foreign_status = BoardStatus.objects.create(
            name="Blocked", board=another_board, position=1000
        )
        self.client.force_authenticate(self.owner)

        response = self.client.post(
            self.task_url,
            {"title": "Wrong status", "status_id": foreign_status.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("status_id", response.data)

    def test_invalid_task_update_rolls_back_all_changes(self):
        task = Task.objects.create(
            title="Original", board_list=self.board_list, created_by=self.owner
        )
        other_board = Board.objects.create(
            name="Mobile", workspace=self.workspace, created_by=self.owner
        )
        foreign_status = BoardStatus.objects.create(
            name="Blocked", board=other_board, position=1000
        )
        self.client.force_authenticate(self.owner)

        response = self.client.patch(
            reverse("task-detail", args=[self.workspace.id, task.id]),
            {"title": "Must not persist", "status_id": foreign_status.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        task.refresh_from_db()
        self.assertEqual(task.title, "Original")

    def test_task_labels_can_be_set_replaced_and_cleared(self):
        self.client.force_authenticate(self.owner)
        create_response = self.client.post(
            self.task_url,
            {"title": "Ship labels", "label_ids": [self.feature_label.id, self.bug_label.id]},
            format="json",
        )

        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            {label["name"] for label in create_response.data["labels"]}, {"Feature", "Bug"}
        )
        task = Task.objects.get(pk=create_response.data["id"])

        detail_url = reverse("task-detail", args=[self.workspace.id, task.id])
        clear_response = self.client.patch(detail_url, {"label_ids": []}, format="json")

        self.assertEqual(clear_response.status_code, status.HTTP_200_OK)
        self.assertEqual(clear_response.data["labels"], [])

    def test_task_rejects_labels_from_another_board(self):
        another_board = Board.objects.create(
            name="Mobile", workspace=self.workspace, created_by=self.owner
        )
        foreign_label = BoardLabel.objects.create(
            name="Feature", color="#2563EB", board=another_board, position=1000
        )
        self.client.force_authenticate(self.owner)

        response = self.client.post(
            self.task_url,
            {"title": "Wrong labels", "label_ids": [foreign_label.id]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("label_ids", response.data)

    def test_task_rejects_an_assignee_from_another_workspace(self):
        self.client.force_authenticate(self.owner)

        response = self.client.post(
            self.task_url,
            {
                "title": "Do not leak",
                "assignee_membership_ids": [self.outsider_board_membership.id],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Task.objects.filter(title="Do not leak").exists())

    def test_task_can_move_to_another_list(self):
        in_progress = BoardList.objects.create(name="In progress", board=self.board, position=2000)
        task = Task.objects.create(
            title="Move me", board_list=self.board_list, created_by=self.owner
        )
        url = reverse("task-move", args=[self.workspace.id, task.id])
        self.client.force_authenticate(self.owner)

        response = self.client.post(url, {"target_list_id": in_progress.id}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        task.refresh_from_db()
        self.assertEqual(task.board_list, in_progress)
        self.assertEqual(task.position, 1000)

    def test_task_can_be_inserted_before_another_task(self):
        first_task = Task.objects.create(
            title="First",
            board_list=self.board_list,
            created_by=self.owner,
            position=1000,
        )
        last_task = Task.objects.create(
            title="Last",
            board_list=self.board_list,
            created_by=self.owner,
            position=2000,
        )
        moving_task = Task.objects.create(
            title="Move",
            board_list=self.board_list,
            created_by=self.owner,
            position=3000,
        )
        url = reverse("task-move", args=[self.workspace.id, moving_task.id])
        self.client.force_authenticate(self.owner)

        response = self.client.post(
            url,
            {"target_list_id": self.board_list.id, "before_task_id": last_task.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        moving_task.refresh_from_db()
        self.assertEqual(moving_task.position, 1500)
        self.assertEqual(first_task.position, 1000)

    def test_task_cannot_use_a_before_task_from_another_list(self):
        another_list = BoardList.objects.create(name="Done", board=self.board, position=2000)
        task = Task.objects.create(title="Move", board_list=self.board_list, created_by=self.owner)
        invalid_before_task = Task.objects.create(
            title="Other list task",
            board_list=another_list,
            created_by=self.owner,
        )
        url = reverse("task-move", args=[self.workspace.id, task.id])
        self.client.force_authenticate(self.owner)

        response = self.client.post(
            url,
            {"target_list_id": self.board_list.id, "before_task_id": invalid_before_task.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_move_rebalances_a_list_when_no_decimal_gap_remains(self):
        source_list = BoardList.objects.create(name="Source", board=self.board, position=2000)
        moving_task = Task.objects.create(
            title="Move", board_list=source_list, created_by=self.owner
        )
        first_target_task = Task.objects.create(
            title="First target",
            board_list=self.board_list,
            created_by=self.owner,
            position=Decimal("0.0000000001"),
        )
        url = reverse("task-move", args=[self.workspace.id, moving_task.id])
        self.client.force_authenticate(self.owner)

        response = self.client.post(
            url,
            {"target_list_id": self.board_list.id, "before_task_id": first_target_task.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        moving_task.refresh_from_db()
        first_target_task.refresh_from_db()
        self.assertEqual(moving_task.position, Decimal("500"))
        self.assertEqual(first_target_task.position, Decimal("1000"))

    def test_board_list_can_move_before_another_list(self):
        second_list = BoardList.objects.create(name="In progress", board=self.board, position=2000)
        third_list = BoardList.objects.create(name="Done", board=self.board, position=3000)
        url = reverse("board-list-move", args=[self.workspace.id, self.board.id, third_list.id])
        self.client.force_authenticate(self.owner)

        response = self.client.post(url, {"before_list_id": second_list.id}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        third_list.refresh_from_db()
        self.assertEqual(third_list.position, Decimal("1500"))

    def test_member_can_retrieve_and_update_a_task(self):
        task = Task.objects.create(
            title="Before", board_list=self.board_list, created_by=self.owner
        )
        url = reverse("task-detail", args=[self.workspace.id, task.id])
        self.client.force_authenticate(self.member)

        description = {
            "type": "doc",
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "New copy"}]}],
        }
        response = self.client.patch(
            url, {"title": "After", "description": description}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        task.refresh_from_db()
        self.assertEqual(task.title, "After")
        self.assertEqual(task.description_content, description)

    def test_task_update_can_replace_assignees(self):
        task = Task.objects.create(
            title="Assign", board_list=self.board_list, created_by=self.owner
        )
        TaskAssignment.objects.create(task=task, membership=self.owner_board_membership)
        url = reverse("task-detail", args=[self.workspace.id, task.id])
        self.client.force_authenticate(self.owner)

        response = self.client.patch(
            url,
            {"assignee_membership_ids": [self.member_board_membership.id]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            list(task.assignments.values_list("membership_id", flat=True)),
            [self.member_board_membership.id],
        )
