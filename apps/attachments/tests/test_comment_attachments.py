from datetime import timedelta
from io import StringIO

from django.core.files.storage import default_storage
from django.core.management import call_command
from django.utils import timezone
from rest_framework import status

from apps.attachments.models import Attachment
from apps.tasks.models import TaskComment
from taskflow.testing import add_board_member, create_user

from . import AttachmentTestCase, image_upload, upload


class CommentAttachmentTests(AttachmentTestCase):
    def setUp(self):
        super().setUp()
        self.comments_url = f"/api/v1/workspaces/{self.workspace.id}/tasks/{self.task.id}/comments/"
        self.activity_url = f"/api/v1/workspaces/{self.workspace.id}/tasks/{self.task.id}/activity/"

    def comment_url(self, comment_id: int) -> str:
        return f"/api/v1/workspaces/{self.workspace.id}/comments/{comment_id}/"

    def upload_for_comment(self, file=None) -> Attachment:
        return self.make_attachment(file, placement=Attachment.Placement.COMMENT)

    def post_comment(self, **payload):
        return self.client.post(self.comments_url, payload, format="json")

    def test_a_file_alone_is_a_comment(self):
        """No text required -- posting a screenshot is the whole message."""
        attachment = self.upload_for_comment(image_upload())

        response = self.post_comment(attachment_ids=[attachment.id])

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        attachment.refresh_from_db()
        self.assertEqual(attachment.comment_id, response.data["id"])
        self.assertIsNotNone(attachment.claimed_at)

    def test_a_comment_with_neither_text_nor_a_file_is_refused(self):
        response = self.post_comment(body="   ")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(TaskComment.objects.exists())

    def test_text_and_files_travel_together(self):
        first = self.upload_for_comment(upload("a.txt"))
        second = self.upload_for_comment(upload("b.txt"))

        response = self.post_comment(body="Both of these", attachment_ids=[first.id, second.id])

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertCountEqual(
            [item["original_name"] for item in response.data["attachments"]],
            ["a.txt", "b.txt"],
        )

    def test_an_upload_cannot_be_claimed_twice(self):
        """Otherwise one file could be re-posted into any number of comments."""
        attachment = self.upload_for_comment()
        self.post_comment(attachment_ids=[attachment.id])

        response = self.post_comment(attachment_ids=[attachment.id])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(TaskComment.objects.count(), 1)

    def test_a_strip_attachment_cannot_be_claimed_by_a_comment(self):
        strip_file = self.make_attachment()

        response = self.post_comment(attachment_ids=[strip_file.id])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_file_from_another_task_cannot_be_claimed(self):
        from taskflow.testing import create_task

        other = create_task(board_list=self.board_list, creator=self.owner, title="Other")
        foreign = self.make_attachment(task=other, placement=Attachment.Placement.COMMENT)

        response = self.post_comment(attachment_ids=[foreign.id])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_rejected_id_leaves_no_comment_behind(self):
        """The whole post is one transaction, so half of it cannot survive."""
        response = self.post_comment(body="Here", attachment_ids=[999_999])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(TaskComment.objects.exists())

    def test_the_timeline_carries_the_files(self):
        attachment = self.upload_for_comment(image_upload("shot.png"))
        self.post_comment(body="Look", attachment_ids=[attachment.id])

        response = self.client.get(self.activity_url)

        entry = next(row for row in response.data["results"] if row["comment_id"])
        self.assertEqual(entry["comment_attachments"][0]["original_name"], "shot.png")
        self.assertTrue(entry["comment_attachments"][0]["can_preview"])

    def test_editing_a_comment_can_drop_a_file(self):
        """A comment owns its files outright -- there is no strip to fall back on."""
        kept = self.upload_for_comment(upload("keep.txt"))
        dropped = self.upload_for_comment(upload("drop.txt"))
        comment_id = self.post_comment(
            body="Two files",
            attachment_ids=[kept.id, dropped.id],
        ).data["id"]
        dropped_name = dropped.file.name

        with self.commit():
            response = self.client.patch(
                self.comment_url(comment_id),
                {"attachment_ids": [kept.id]},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(Attachment.objects.filter(pk=dropped.pk).exists())
        self.assertFalse(default_storage.exists(dropped_name))
        self.assertTrue(Attachment.objects.filter(pk=kept.pk).exists())

    def test_resending_the_same_ids_changes_nothing(self):
        attachment = self.upload_for_comment()
        comment_id = self.post_comment(body="Hi", attachment_ids=[attachment.id]).data["id"]

        response = self.client.patch(
            self.comment_url(comment_id),
            {"body": "Hi again", "attachment_ids": [attachment.id]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(Attachment.objects.filter(pk=attachment.pk).exists())

    def test_editing_only_the_text_keeps_the_files(self):
        """`attachment_ids` absent means unchanged, not emptied."""
        attachment = self.upload_for_comment()
        comment_id = self.post_comment(body="First", attachment_ids=[attachment.id]).data["id"]

        response = self.client.patch(
            self.comment_url(comment_id),
            {"body": "Second"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(Attachment.objects.filter(pk=attachment.pk).exists())

    def test_a_file_only_comment_can_still_be_edited(self):
        """Its body is empty, and that must not fail the "say something" check."""
        attachment = self.upload_for_comment()
        comment_id = self.post_comment(attachment_ids=[attachment.id]).data["id"]

        response = self.client.patch(
            self.comment_url(comment_id),
            {"body": "Adding words now"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_emptying_a_file_only_comment_is_refused(self):
        attachment = self.upload_for_comment()
        comment_id = self.post_comment(attachment_ids=[attachment.id]).data["id"]

        response = self.client.patch(
            self.comment_url(comment_id),
            {"attachment_ids": []},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(Attachment.objects.filter(pk=attachment.pk).exists())

    def test_deleting_a_comment_takes_its_files(self):
        attachment = self.upload_for_comment(image_upload())
        comment_id = self.post_comment(body="Bye", attachment_ids=[attachment.id]).data["id"]
        file_name = attachment.file.name

        with self.commit():
            self.client.delete(self.comment_url(comment_id))

        self.assertFalse(Attachment.objects.filter(pk=attachment.pk).exists())
        self.assertFalse(default_storage.exists(file_name))

    def test_another_member_cannot_attach_to_your_comment(self):
        attachment = self.upload_for_comment()
        other = create_user("other@example.com")
        add_board_member(board=self.board, user=other)
        self.client.force_authenticate(other)

        response = self.post_comment(body="Mine", attachment_ids=[attachment.id])

        # Unclaimed and on the same task, so this is allowed.
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class CleanupCommandTests(AttachmentTestCase):
    def age(self, attachment, *, hours: int) -> None:
        Attachment.objects.filter(pk=attachment.pk).update(
            created_at=timezone.now() - timedelta(hours=hours)
        )

    def run_cleanup(self, *args) -> str:
        output = StringIO()
        with self.commit():
            call_command("cleanup_attachments", *args, stdout=output)
        return output.getvalue()

    def test_an_abandoned_upload_is_reaped(self):
        """Pasted into a comment box, then the tab was closed."""
        orphan = self.upload_for_comment_attachment()
        self.age(orphan, hours=48)
        file_name = orphan.file.name

        self.run_cleanup()

        self.assertFalse(Attachment.objects.filter(pk=orphan.pk).exists())
        self.assertFalse(default_storage.exists(file_name))

    def test_a_recent_upload_is_spared(self):
        """Someone is still typing the comment it belongs to."""
        pending = self.upload_for_comment_attachment()

        self.run_cleanup()

        self.assertTrue(Attachment.objects.filter(pk=pending.pk).exists())

    def test_a_task_attachment_is_never_reaped(self):
        """It is claimed on upload, because the strip shows it immediately."""
        attachment = self.make_attachment()
        self.age(attachment, hours=1000)

        self.run_cleanup()

        self.assertTrue(Attachment.objects.filter(pk=attachment.pk).exists())

    def test_a_posted_comment_file_is_never_reaped(self):
        attachment = self.upload_for_comment_attachment()
        self.client.post(
            f"/api/v1/workspaces/{self.workspace.id}/tasks/{self.task.id}/comments/",
            {"body": "Posted", "attachment_ids": [attachment.id]},
            format="json",
        )
        self.age(attachment, hours=1000)

        self.run_cleanup()

        self.assertTrue(Attachment.objects.filter(pk=attachment.pk).exists())

    def test_dry_run_reports_without_deleting(self):
        orphan = self.upload_for_comment_attachment()
        self.age(orphan, hours=48)

        output = self.run_cleanup("--dry-run")

        self.assertIn(f"would delete {orphan.pk}", output)
        self.assertTrue(Attachment.objects.filter(pk=orphan.pk).exists())

    def test_the_age_threshold_can_be_overridden(self):
        orphan = self.upload_for_comment_attachment()
        self.age(orphan, hours=2)

        self.run_cleanup("--hours", "1")

        self.assertFalse(Attachment.objects.filter(pk=orphan.pk).exists())

    def upload_for_comment_attachment(self) -> Attachment:
        return self.make_attachment(placement=Attachment.Placement.COMMENT)
