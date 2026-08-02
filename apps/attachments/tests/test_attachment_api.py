from unittest.mock import patch

from django.core.files.storage import default_storage
from django.db import transaction
from django.test import override_settings
from rest_framework import status

from apps.attachments.models import Attachment
from apps.boards.models import BoardMembership
from taskflow.testing import (
    add_board_member,
    create_board,
    create_board_list,
    create_task,
    create_user,
    create_workspace,
)

from . import AttachmentTestCase, image_upload, upload


class AttachmentUploadTests(AttachmentTestCase):
    def test_upload_stores_the_file_and_returns_it(self):
        response = self.post_upload(upload("notes.txt", b"hello there"))

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["original_name"], "notes.txt")
        self.assertEqual(response.data["content_type"], "text/plain")
        self.assertEqual(response.data["size"], len(b"hello there"))
        self.assertEqual(response.data["placement"], Attachment.Placement.TASK)

        attachment = Attachment.objects.get(pk=response.data["id"])
        self.assertTrue(default_storage.exists(attachment.file.name))
        self.assertEqual(attachment.file.read(), b"hello there")

    def test_task_upload_is_claimed_immediately(self):
        """The strip shows it at once, so it is never a candidate for reaping."""
        attachment = self.make_attachment()
        self.assertIsNotNone(attachment.claimed_at)

    def test_comment_upload_waits_to_be_claimed(self):
        attachment = self.make_attachment(placement=Attachment.Placement.COMMENT)
        self.assertIsNone(attachment.claimed_at)
        self.assertIsNone(attachment.comment_id)

    def test_client_filename_never_becomes_a_path(self):
        """A browser-supplied name must not steer where bytes are written."""
        attachment = self.make_attachment(upload("../../etc/passwd.txt", b"x"))

        self.assertEqual(attachment.original_name, "passwd.txt")
        self.assertNotIn("..", attachment.file.name)
        self.assertTrue(attachment.file.name.startswith("attachments/files/"))
        self.assertTrue(attachment.file.name.endswith(".txt"))

    def test_two_uploads_of_one_name_do_not_collide(self):
        first = self.make_attachment(upload("same.txt", b"one"))
        second = self.make_attachment(upload("same.txt", b"two"))

        self.assertNotEqual(first.file.name, second.file.name)
        self.assertEqual(first.file.read(), b"one")
        self.assertEqual(second.file.read(), b"two")

    @override_settings(ATTACHMENT_MAX_BYTES=1024)
    def test_upload_over_the_limit_is_rejected(self):
        response = self.post_upload(upload("big.bin", b"x" * 2048))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("too large", str(response.data["file"][0]).lower())
        self.assertFalse(Attachment.objects.exists())

    @override_settings(ATTACHMENT_MAX_BYTES=1024)
    def test_a_body_over_the_limit_is_refused_before_it_is_parsed(self):
        """The serializer's own check runs after the parser has spooled to disk."""
        with patch(
            "rest_framework.parsers.MultiPartParser.parse", side_effect=AssertionError
        ) as parse:
            response = self.post_upload(upload("huge.bin", b"x" * 64 * 1024))

        self.assertEqual(response.status_code, status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        self.assertIn("too large", str(response.data["detail"]).lower())
        parse.assert_not_called()
        self.assertFalse(Attachment.objects.exists())

    def test_empty_upload_is_rejected(self):
        response = self.post_upload(upload("empty.txt", b""))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Attachment.objects.exists())

    def test_viewer_cannot_upload(self):
        self.client.force_authenticate(self.viewer)

        self.assertEqual(self.post_upload().status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(Attachment.objects.exists())

    def test_outsider_cannot_upload(self):
        self.client.force_authenticate(self.outsider)

        self.assertEqual(self.post_upload().status_code, status.HTTP_404_NOT_FOUND)

    def test_a_task_from_another_workspace_cannot_be_targeted(self):
        """The task id is checked against the workspace in the URL, not trusted."""
        other_workspace = create_workspace(owner=self.outsider, name="Theirs")
        other_task = create_task(
            board_list=create_board_list(
                board=create_board(workspace=other_workspace, creator=self.outsider)
            ),
            creator=self.outsider,
        )

        self.assertEqual(self.post_upload(task=other_task).status_code, status.HTTP_404_NOT_FOUND)

    def test_upload_records_activity(self):
        self.make_attachment(upload("plan.txt"))

        actions = list(self.task.activity.values_list("action", "payload"))
        self.assertIn(("attachment_added", {"name": "plan.txt"}), actions)


class ContentTypeTests(AttachmentTestCase):
    def test_octet_stream_falls_back_to_the_filename(self):
        """Browsers routinely send a useless type; the extension knows better."""
        attachment = self.make_attachment(
            upload("report.pdf", b"%PDF-1.4", content_type="application/octet-stream")
        )
        self.assertEqual(attachment.content_type, "application/pdf")

    def test_unparseable_content_type_is_discarded(self):
        attachment = self.make_attachment(
            upload("thing.bin", b"x", content_type="not a mime type at all")
        )
        self.assertEqual(attachment.content_type, "application/octet-stream")


class ThumbnailTests(AttachmentTestCase):
    def test_raster_image_gets_a_thumbnail(self):
        attachment = self.make_attachment(image_upload())

        self.assertTrue(attachment.thumbnail)
        self.assertTrue(default_storage.exists(attachment.thumbnail.name))
        self.assertTrue(attachment.thumbnail.name.endswith(".webp"))

    def test_thumbnail_url_is_offered_only_when_one_exists(self):
        with_thumbnail = self.post_upload(image_upload())
        without = self.post_upload(upload("notes.txt"))

        self.assertIn("variant=thumb", with_thumbnail.data["thumbnail_url"])
        self.assertIsNone(without.data["thumbnail_url"])

    def test_svg_is_never_rasterized(self):
        """SVG is a scriptable document, so it is neither previewed nor drawn."""
        attachment = self.make_attachment(
            upload("logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml")
        )

        self.assertFalse(attachment.thumbnail)

    def test_a_corrupt_image_still_uploads(self):
        attachment = self.make_attachment(upload("broken.png", b"not a png", "image/png"))

        self.assertFalse(attachment.thumbnail)
        self.assertTrue(default_storage.exists(attachment.file.name))

    @override_settings(ATTACHMENT_THUMBNAIL_MAX_PIXELS=10)
    def test_an_image_with_too_many_pixels_uploads_without_a_preview(self):
        attachment = self.make_attachment(image_upload())

        self.assertFalse(attachment.thumbnail)
        self.assertTrue(default_storage.exists(attachment.file.name))

    def test_a_decompression_bomb_uploads_without_a_preview(self):
        """Pillow raises DecompressionBombError, which is not an OSError."""
        with patch("PIL.Image.MAX_IMAGE_PIXELS", 1):
            attachment = self.make_attachment(image_upload())

        self.assertFalse(attachment.thumbnail)
        self.assertTrue(default_storage.exists(attachment.file.name))

    def test_the_original_survives_thumbnailing(self):
        """Pillow reads the upload; the stored bytes must be untouched by it."""
        attachment = self.make_attachment(image_upload())

        self.assertEqual(attachment.size, len(attachment.file.read()))


class AttachmentDownloadTests(AttachmentTestCase):
    def test_download_returns_the_bytes(self):
        attachment = self.make_attachment(upload("notes.txt", b"the contents"))

        response = self.client.get(self.detail_url(attachment))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(b"".join(response.streaming_content), b"the contents")
        self.assertEqual(response["Content-Type"], "text/plain")

    def test_allowlisted_types_render_in_place(self):
        attachment = self.make_attachment(image_upload("photo.png"))

        response = self.client.get(self.detail_url(attachment))

        self.assertTrue(response["Content-Disposition"].startswith("inline;"))
        self.assertIn('filename="photo.png"', response["Content-Disposition"])

    def test_everything_else_is_forced_to_download(self):
        attachment = self.make_attachment(upload("page.html", b"<script>x</script>", "text/html"))

        response = self.client.get(self.detail_url(attachment))

        self.assertTrue(response["Content-Disposition"].startswith("attachment;"))

    def test_svg_is_forced_to_download(self):
        """Served inline it would run script in the API's own origin."""
        attachment = self.make_attachment(
            upload("logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml")
        )

        response = self.client.get(self.detail_url(attachment))

        self.assertTrue(response["Content-Disposition"].startswith("attachment;"))

    def test_download_flag_overrides_inline(self):
        attachment = self.make_attachment(image_upload())

        response = self.client.get(f"{self.detail_url(attachment)}?download=1")

        self.assertTrue(response["Content-Disposition"].startswith("attachment;"))

    def test_responses_forbid_content_sniffing(self):
        attachment = self.make_attachment(upload("notes.txt"))

        response = self.client.get(self.detail_url(attachment))

        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_non_ascii_names_survive_the_header(self):
        attachment = self.make_attachment(upload("отчёт.txt", b"x"))

        disposition = self.client.get(self.detail_url(attachment))["Content-Disposition"]

        self.assertIn("filename*=UTF-8''", disposition)
        self.assertIn("%D0%BE", disposition)

    def test_thumb_variant_serves_the_derivative(self):
        attachment = self.make_attachment(image_upload())

        response = self.client.get(f"{self.detail_url(attachment)}?variant=thumb")

        self.assertEqual(response["Content-Type"], "image/webp")
        self.assertLess(
            len(b"".join(response.streaming_content)),
            attachment.size + 1024,
        )

    def test_thumb_variant_falls_back_when_there_is_none(self):
        attachment = self.make_attachment(upload("notes.txt", b"plain"))

        response = self.client.get(f"{self.detail_url(attachment)}?variant=thumb")

        self.assertEqual(response["Content-Type"], "text/plain")

    def test_a_viewer_may_read(self):
        attachment = self.make_attachment()
        self.client.force_authenticate(self.viewer)

        self.assertEqual(
            self.client.get(self.detail_url(attachment)).status_code,
            status.HTTP_200_OK,
        )

    def test_a_non_member_may_not_read(self):
        attachment = self.make_attachment()
        self.client.force_authenticate(self.outsider)

        self.assertEqual(
            self.client.get(self.detail_url(attachment)).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_signing_out_hides_the_file(self):
        attachment = self.make_attachment()
        self.client.force_authenticate(None)

        self.assertIn(
            self.client.get(self.detail_url(attachment)).status_code,
            {status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN},
        )

    def test_an_archived_task_keeps_its_files_readable(self):
        """It still renders in the archive, and it can be restored."""
        attachment = self.make_attachment()
        self.client.delete(self.task_url())

        self.assertEqual(
            self.client.get(self.detail_url(attachment)).status_code,
            status.HTTP_200_OK,
        )

    def test_an_archived_list_hides_them(self):
        attachment = self.make_attachment()
        self.board_list.is_archived = True
        self.board_list.save(update_fields=["is_archived"])

        self.assertEqual(
            self.client.get(self.detail_url(attachment)).status_code,
            status.HTTP_404_NOT_FOUND,
        )


class RangeRequestTests(AttachmentTestCase):
    """Video cannot be scrubbed without them, and Safari will not play at all."""

    def setUp(self):
        super().setUp()
        self.attachment = self.make_attachment(
            upload("clip.mp4", b"0123456789", content_type="video/mp4")
        )

    def test_media_advertises_range_support(self):
        response = self.client.get(self.detail_url(self.attachment))

        self.assertEqual(response["Accept-Ranges"], "bytes")

    def test_a_range_returns_just_that_slice(self):
        response = self.client.get(self.detail_url(self.attachment), headers={"range": "bytes=2-5"})

        self.assertEqual(response.status_code, 206)
        self.assertEqual(b"".join(response.streaming_content), b"2345")
        self.assertEqual(response["Content-Range"], "bytes 2-5/10")
        self.assertEqual(response["Content-Length"], "4")

    def test_an_open_ended_range_runs_to_the_end(self):
        response = self.client.get(self.detail_url(self.attachment), headers={"range": "bytes=7-"})

        self.assertEqual(response.status_code, 206)
        self.assertEqual(b"".join(response.streaming_content), b"789")

    def test_a_suffix_range_counts_back_from_the_end(self):
        response = self.client.get(self.detail_url(self.attachment), headers={"range": "bytes=-3"})

        self.assertEqual(response.status_code, 206)
        self.assertEqual(b"".join(response.streaming_content), b"789")

    def test_a_range_past_the_end_is_unsatisfiable(self):
        response = self.client.get(
            self.detail_url(self.attachment),
            headers={"range": "bytes=99-120"},
        )

        self.assertEqual(response.status_code, 416)
        self.assertEqual(response["Content-Range"], "bytes */10")

    def test_ranges_are_ignored_for_non_media(self):
        """A whole-file reply is always valid, and images never seek."""
        text = self.make_attachment(upload("notes.txt", b"0123456789"))

        response = self.client.get(self.detail_url(text), headers={"range": "bytes=2-5"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)


class AttachmentDeleteTests(AttachmentTestCase):
    def test_delete_removes_the_row_and_the_bytes(self):
        attachment = self.make_attachment(image_upload())
        file_name, thumbnail_name = attachment.file.name, attachment.thumbnail.name

        with self.commit():
            response = self.client.delete(self.detail_url(attachment))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Attachment.objects.filter(pk=attachment.pk).exists())
        self.assertFalse(default_storage.exists(file_name))
        self.assertFalse(default_storage.exists(thumbnail_name))

    def test_delete_records_activity(self):
        attachment = self.make_attachment(upload("plan.txt"))

        self.client.delete(self.detail_url(attachment))

        self.assertIn(
            ("attachment_removed", {"name": "plan.txt"}),
            list(self.task.activity.values_list("action", "payload")),
        )

    def test_any_board_editor_may_delete_a_task_attachment(self):
        """Strip files are task content, and task content is shared."""
        attachment = self.make_attachment()
        editor = self.viewer
        BoardMembership.objects.filter(board=self.board, user=editor).update(
            role=BoardMembership.Role.MEMBER
        )
        self.client.force_authenticate(editor)

        self.assertEqual(
            self.client.delete(self.detail_url(attachment)).status_code,
            status.HTTP_204_NO_CONTENT,
        )

    def test_a_viewer_may_not_delete(self):
        attachment = self.make_attachment()
        self.client.force_authenticate(self.viewer)

        self.assertEqual(
            self.client.delete(self.detail_url(attachment)).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertTrue(Attachment.objects.filter(pk=attachment.pk).exists())

    def test_only_the_author_may_delete_a_comment_attachment(self):
        """Comment files follow their comment, which is author-only to edit."""
        editor = create_user("editor@example.com")
        add_board_member(board=self.board, user=editor, role=BoardMembership.Role.MEMBER)
        self.client.force_authenticate(editor)
        attachment = self.make_attachment(placement=Attachment.Placement.COMMENT)

        self.client.force_authenticate(self.owner)
        response = self.client.delete(self.detail_url(attachment))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Attachment.objects.filter(pk=attachment.pk).exists())

    def test_cascading_from_a_task_removes_the_bytes(self):
        """The reason cleanup is a signal: nothing called a delete service here."""
        attachment = self.make_attachment(image_upload())
        file_name = attachment.file.name

        with self.commit():
            self.task.delete()

        self.assertFalse(default_storage.exists(file_name))

    def test_a_rolled_back_delete_keeps_the_file(self):
        """Deferring to commit is what stops a rollback from destroying data."""
        attachment = self.make_attachment()
        file_name = attachment.file.name
        attachment_id = attachment.pk

        with transaction.atomic():
            attachment.delete()
            transaction.set_rollback(True)

        self.assertTrue(default_storage.exists(file_name))
        self.assertTrue(Attachment.objects.filter(pk=attachment_id).exists())
