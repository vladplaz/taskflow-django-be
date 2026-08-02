"""Shared fixtures for the attachment suites."""

import io
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APITestCase

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


def png_bytes(*, size=(8, 6)) -> bytes:
    """A real PNG, so Pillow has something genuine to thumbnail."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, (120, 80, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def upload(name="notes.txt", content=b"hello", content_type="text/plain") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type=content_type)


def image_upload(name="photo.png") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, png_bytes(), content_type="image/png")


class AttachmentTestCase(APITestCase):
    """A board with an admin, a viewer, an outsider, and one task on it."""

    def setUp(self):
        # One directory per test, so no case asserts on another's files.
        media_root = tempfile.mkdtemp(prefix="taskflow-attachments-")
        media_override = override_settings(MEDIA_ROOT=media_root)
        media_override.enable()
        self.addCleanup(media_override.disable)
        self.addCleanup(shutil.rmtree, media_root, True)

        self.owner = create_user("owner@example.com")
        self.viewer = create_user("viewer@example.com")
        self.outsider = create_user("outsider@example.com")

        self.workspace = create_workspace(owner=self.owner)
        self.board = create_board(workspace=self.workspace, creator=self.owner)
        add_board_member(board=self.board, user=self.viewer, role=BoardMembership.Role.VIEWER)
        self.board_list = create_board_list(board=self.board)
        self.task = create_task(board_list=self.board_list, creator=self.owner)

        self.client.force_authenticate(self.owner)

    def upload_url(self, task=None) -> str:
        task_id = (task or self.task).id
        return f"/api/v1/workspaces/{self.workspace.id}/tasks/{task_id}/attachments/"

    def detail_url(self, attachment) -> str:
        attachment_id = getattr(attachment, "id", attachment)
        return f"/api/v1/workspaces/{self.workspace.id}/attachments/{attachment_id}/"

    def task_url(self, task=None) -> str:
        return f"/api/v1/workspaces/{self.workspace.id}/tasks/{(task or self.task).id}/"

    def post_upload(self, file=None, *, placement=Attachment.Placement.TASK, task=None):
        return self.client.post(
            self.upload_url(task),
            {"file": file if file is not None else upload(), "placement": placement},
            format="multipart",
        )

    def make_attachment(self, file=None, **kwargs) -> Attachment:
        """Upload through the API, so rows under test always look like real ones."""
        response = self.post_upload(file, **kwargs)
        assert response.status_code == 201, response.data
        return Attachment.objects.get(pk=response.data["id"])

    def commit(self):
        """Run the on-commit callbacks this test's transaction would defer.

        Removing bytes waits for the commit (see `apps/attachments/signals.py`),
        which `TestCase` never reaches.
        """
        return self.captureOnCommitCallbacks(execute=True)
