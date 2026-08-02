from django.conf import settings
from django.db import models

from apps.tasks.models import Task, TaskComment
from taskflow.models import CreatedAtModel

from .storage import attachment_upload_to, thumbnail_upload_to


class Attachment(CreatedAtModel):
    """One uploaded file belonging to a task, or to one of its comments.

    Uploading is a step of its own; everything else refers to the row by id.
    """

    class Placement(models.TextChoices):
        #: Listed in the task's strip, which owns it even if the
        #: description also references it.
        TASK = "task", "Task"
        #: Belongs to a single comment and is shown with it.
        COMMENT = "comment", "Comment"

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="attachments")
    comment = models.ForeignKey(
        TaskComment,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    placement = models.CharField(max_length=16, choices=Placement)
    file = models.FileField(upload_to=attachment_upload_to, max_length=500)
    thumbnail = models.FileField(
        upload_to=thumbnail_upload_to,
        max_length=500,
        blank=True,
    )
    #: Never part of the storage key -- see `storage.attachment_upload_to`.
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=255)
    size = models.PositiveBigIntegerField()
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="uploaded_attachments",
    )
    #: When the upload became part of something a user can see. A file
    #: pasted into a composer that is never posted stays NULL forever;
    #: `cleanup_attachments` reaps those.
    claimed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(
                fields=["task", "placement", "created_at"],
                name="attachment_task_idx",
            ),
            models.Index(fields=["comment"], name="attachment_comment_idx"),
            models.Index(fields=["claimed_at", "created_at"], name="attachment_unclaimed_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(placement="task", comment__isnull=True)
                | models.Q(placement="comment"),
                name="task_attachment_has_no_comment",
            ),
        ]

    def __str__(self) -> str:
        return self.original_name

    @property
    def has_thumbnail(self) -> bool:
        return bool(self.thumbnail)
