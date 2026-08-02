"""One code path over local disk and S3. boto3 signs offline, so the S3 branch
needs no bucket and no network."""

from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, default_storage, storages
from django.test import override_settings
from rest_framework import status
from storages.backends.s3 import S3Storage

from apps.attachments.storage import (
    content_disposition,
    is_inline_renderable,
    resolve_content_type,
    signed_url,
)

from . import AttachmentTestCase, image_upload, upload


S3_STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": "taskflow-test-bucket",
            "region_name": "eu-central-1",
            "access_key": "AKIAIOSFODNN7EXAMPLE",
            "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "querystring_auth": True,
            "querystring_expire": 300,
        },
    },
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


class SignedUrlTests(AttachmentTestCase):
    def test_local_disk_reports_that_it_cannot_sign(self):
        """Which is the signal to stream, not an error to handle."""
        self.assertIsNone(
            signed_url(
                storage=FileSystemStorage(location=self._get_media_root()),
                name="attachments/files/1/abc.txt",
                disposition='attachment; filename="a.txt"',
                content_type="text/plain",
            )
        )

    def test_s3_signs_a_url_carrying_the_response_headers(self):
        url = signed_url(
            storage=S3Storage(**S3_STORAGES["default"]["OPTIONS"]),
            name="attachments/files/1/abc.pdf",
            disposition='inline; filename="report.pdf"',
            content_type="application/pdf",
        )

        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        self.assertIn("taskflow-test-bucket", parsed.netloc + parsed.path)
        self.assertIn("attachments/files/1/abc.pdf", parsed.path)
        # Presigned, so the bucket needs no public read policy.
        self.assertIn("X-Amz-Signature", query)
        self.assertEqual(query["X-Amz-Expires"], ["300"])
        # The object arrives named, rather than as its opaque UUID key.
        self.assertEqual(
            query["response-content-disposition"],
            ['inline; filename="report.pdf"'],
        )
        self.assertEqual(query["response-content-type"], ["application/pdf"])

    def _get_media_root(self) -> str:
        from django.conf import settings

        return str(settings.MEDIA_ROOT)


class S3ServingTests(AttachmentTestCase):
    """The view redirects instead of streaming when the backend can sign."""

    def setUp(self):
        super().setUp()
        self.attachment = self.make_attachment(image_upload("diagram.png"))

    def _with_s3(self):
        # Only the serving backend is swapped; the bytes stay on disk.
        override = override_settings(STORAGES=S3_STORAGES)
        override.enable()
        self.addCleanup(override.disable)
        storages._storages.clear()
        self.addCleanup(storages._storages.clear)

    def test_serving_redirects_to_the_bucket(self):
        self._with_s3()

        response = self.client.get(self.detail_url(self.attachment))

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertIn("taskflow-test-bucket", response["Location"])
        self.assertIn("X-Amz-Signature", response["Location"])

    def test_the_redirect_is_never_cached(self):
        """Its target expires; a cached 302 would outlive it."""
        self._with_s3()

        response = self.client.get(self.detail_url(self.attachment))

        self.assertEqual(response["Cache-Control"], "no-store")

    def test_the_download_flag_reaches_the_bucket(self):
        self._with_s3()

        response = self.client.get(f"{self.detail_url(self.attachment)}?download=1")

        location = parse_qs(urlparse(response["Location"]).query)
        self.assertTrue(location["response-content-disposition"][0].startswith("attachment;"))

    def test_the_thumb_variant_redirects_to_the_derivative(self):
        self._with_s3()

        response = self.client.get(f"{self.detail_url(self.attachment)}?variant=thumb")

        self.assertIn("attachments/thumbnails/", urlparse(response["Location"]).path)

    def test_a_javascript_read_is_streamed_instead_of_redirected(self):
        """The Office and text previews cannot follow a redirect to a bucket.

        Their `fetch` carries credentials, which survive the redirect, and S3
        never answers a credentialed cross-origin read.
        """
        with patch("apps.attachments.views.signed_url") as sign:
            response = self.client.get(f"{self.detail_url(self.attachment)}?stream=1")

        sign.assert_not_called()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("Location", response)

    def test_streaming_still_names_the_file(self):
        """Whatever the redirect would have carried, the stream carries too."""
        with patch("apps.attachments.views.signed_url"):
            response = self.client.get(f"{self.detail_url(self.attachment)}?stream=1&download=1")

        self.assertTrue(response["Content-Disposition"].startswith("attachment;"))
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_access_is_still_checked_before_any_url_is_minted(self):
        """The redirect must never be the thing that decides who may read."""
        self._with_s3()
        self.client.force_authenticate(self.outsider)

        response = self.client.get(self.detail_url(self.attachment))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ContentTypePolicyTests(AttachmentTestCase):
    def test_the_inline_list_excludes_what_can_execute(self):
        for content_type in ("image/png", "video/mp4", "application/pdf", "text/plain"):
            self.assertTrue(is_inline_renderable(content_type), content_type)

        for content_type in (
            "text/html",
            "image/svg+xml",
            "application/xhtml+xml",
            "application/javascript",
            "application/octet-stream",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ):
            self.assertFalse(is_inline_renderable(content_type), content_type)

    def test_a_pdf_can_be_framed_by_the_client_but_not_by_anyone_else(self):
        """The viewer previews PDFs in an iframe, which DENY would block."""
        attachment = self.make_attachment(upload("report.pdf", b"%PDF-1.4", "application/pdf"))

        response = self.client.get(self.detail_url(attachment))

        self.assertNotIn("X-Frame-Options", response)
        self.assertIn("frame-ancestors", response["Content-Security-Policy"])
        self.assertIn("http://localhost:5173", response["Content-Security-Policy"])

    def test_ordinary_api_responses_still_deny_framing(self):
        """The exemption is scoped to file bytes, not to the API at large."""
        response = self.client.get(self.task_url())

        self.assertEqual(response["X-Frame-Options"], "DENY")

    def test_a_header_parameter_cannot_be_injected_through_a_filename(self):
        header = content_disposition(filename='evil";x="y', inline=False)

        self.assertNotIn('";x="', header.split("filename*=")[0])

    def test_a_filename_with_no_ascii_still_names_the_download(self):
        header = content_disposition(filename="报告", inline=False)

        self.assertIn('filename="download"', header)
        self.assertIn("filename*=UTF-8''", header)

    def test_content_type_parameters_are_stripped(self):
        self.assertEqual(
            resolve_content_type(declared="text/plain; charset=utf-8", filename="a.txt"),
            "text/plain",
        )

    def test_an_unknown_extension_falls_back_rather_than_guessing(self):
        self.assertEqual(
            resolve_content_type(declared=None, filename="archive.zzz"),
            "application/octet-stream",
        )


class StorageFailureTests(AttachmentTestCase):
    def test_a_failed_file_removal_does_not_break_the_delete(self):
        """The row is already gone by then; raising would only mean a 500."""
        attachment = self.make_attachment(upload("notes.txt"))

        with patch.object(
            default_storage.__class__,
            "delete",
            side_effect=OSError("bucket unreachable"),
        ):
            with self.commit():
                response = self.client.delete(self.detail_url(attachment))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_a_row_that_outlived_its_bytes_is_a_404(self):
        """A restored database or a half-finished storage move, not a bug."""
        attachment = self.make_attachment(upload("notes.txt"))
        default_storage.delete(attachment.file.name)

        response = self.client.get(self.detail_url(attachment))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_thumbnails_and_originals_are_stored_apart(self):
        attachment = self.make_attachment(image_upload())

        self.assertTrue(attachment.file.name.startswith("attachments/files/"))
        self.assertTrue(attachment.thumbnail.name.startswith("attachments/thumbnails/"))

    def test_keys_are_scoped_to_the_task(self):
        attachment = self.make_attachment(ContentFile(b"x", name="a.txt"))

        self.assertIn(f"/{self.task.id}/", attachment.file.name)
