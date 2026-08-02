from django.conf import settings
from django.urls import reverse
from rest_framework import serializers

from .models import Attachment
from .storage import is_inline_renderable


class AttachmentSerializer(serializers.ModelSerializer):
    """How an attachment is described to the client.

    No storage URL: the client gets the API path, which is access-checked on
    every read and stable across backends.
    """

    url = serializers.SerializerMethodField()
    thumbnail_url = serializers.SerializerMethodField()
    can_preview = serializers.SerializerMethodField()
    uploaded_by_id = serializers.IntegerField(read_only=True, allow_null=True)
    uploaded_by_display_name = serializers.CharField(
        source="uploaded_by.display_name",
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = Attachment
        fields = [
            "id",
            "original_name",
            "content_type",
            "size",
            "url",
            "thumbnail_url",
            "can_preview",
            "placement",
            "uploaded_by_id",
            "uploaded_by_display_name",
            "created_at",
        ]
        read_only_fields = fields

    def _workspace_id(self, attachment: Attachment) -> int:
        # Supplied by the view, or every row walks back up to the workspace.
        workspace_id = self.context.get("workspace_id")
        if workspace_id is not None:
            return workspace_id
        return attachment.task.board_list.board.workspace_id

    def get_url(self, attachment: Attachment) -> str:
        return reverse(
            "attachment-detail",
            kwargs={"workspace_id": self._workspace_id(attachment), "pk": attachment.pk},
        )

    def get_thumbnail_url(self, attachment: Attachment) -> str | None:
        if not attachment.thumbnail:
            return None
        return f"{self.get_url(attachment)}?variant=thumb"

    def get_can_preview(self, attachment: Attachment) -> bool:
        """Whether the viewer can render this file rather than offer it.

        The server decides, since the server is what refuses to serve anything
        else inline.
        """
        return is_inline_renderable(attachment.content_type)


class AttachmentUploadSerializer(serializers.Serializer):
    """Validates one multipart upload before anything touches storage."""

    file = serializers.FileField()
    placement = serializers.ChoiceField(
        choices=Attachment.Placement,
        default=Attachment.Placement.TASK,
    )

    def validate_file(self, upload):
        if upload.size == 0:
            raise serializers.ValidationError("This file is empty.")
        if upload.size > settings.ATTACHMENT_MAX_BYTES:
            limit_mb = settings.ATTACHMENT_MAX_BYTES / (1024 * 1024)
            actual_mb = upload.size / (1024 * 1024)
            raise serializers.ValidationError(
                f"This file is too large ({actual_mb:.1f} MB, limit {limit_mb:.0f} MB)."
            )
        if not upload.name:
            raise serializers.ValidationError("This file has no name.")
        return upload
