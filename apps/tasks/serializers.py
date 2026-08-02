import json

from django.conf import settings
from django.db import transaction
from django.db.models import Prefetch, prefetch_related_objects
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.attachments.models import Attachment
from apps.attachments.serializers import AttachmentSerializer
from apps.attachments.services import (
    replace_comment_attachments,
    sync_description_attachments,
)
from apps.boards.models import BoardLabel, BoardStatus

from .models import BoardList, Task, TaskActivity, TaskAssignment, TaskComment
from .services import (
    get_task_comment_mentions,
    record_task_activity,
    replace_task_assignments,
    replace_task_labels,
    replace_task_status,
)


#: The task's own files. Comment attachments share the relation but belong to
#: the timeline, not the strip.
STRIP_ATTACHMENT_PREFETCH = Prefetch(
    "attachments",
    queryset=Attachment.objects.filter(placement=Attachment.Placement.TASK).select_related(
        "uploaded_by"
    ),
    to_attr="strip_attachments",
)


class BoardListSerializer(serializers.ModelSerializer):
    class Meta:
        model = BoardList
        fields = ["id", "name", "position", "created_at"]
        read_only_fields = ["id", "position", "created_at"]


class TaskAssigneeSerializer(serializers.ModelSerializer):
    membership_id = serializers.IntegerField(source="membership.id", read_only=True)
    user_id = serializers.IntegerField(source="membership.user.id", read_only=True)
    display_name = serializers.CharField(
        source="membership.user.display_name",
        read_only=True,
    )

    class Meta:
        model = TaskAssignment
        fields = ["membership_id", "user_id", "display_name", "assigned_at"]


class TaskLabelSerializer(serializers.ModelSerializer):
    class Meta:
        model = BoardLabel
        fields = ["id", "name", "color"]


class TaskStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = BoardStatus
        fields = ["id", "name"]


class BoundedJSONField(serializers.JSONField):
    """A JSON field with an upper bound on serialized size."""

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        size = len(json.dumps(value))
        if size > settings.TASK_DESCRIPTION_MAX_BYTES:
            raise serializers.ValidationError(
                f"This description is too large "
                f"({size} bytes, limit {settings.TASK_DESCRIPTION_MAX_BYTES})."
            )
        return value


class BaseTaskSerializer(serializers.ModelSerializer):
    """Field definitions shared by the list and detail task representations."""

    assignees = TaskAssigneeSerializer(source="assignments", many=True, read_only=True)
    labels = TaskLabelSerializer(many=True, read_only=True)
    status = TaskStatusSerializer(read_only=True)
    description = BoundedJSONField(source="description_content", required=False)
    assignee_membership_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        write_only=True,
    )
    status_id = serializers.IntegerField(required=False, allow_null=True)
    label_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        write_only=True,
    )
    attachment_count = serializers.SerializerMethodField()

    class Meta:
        model = Task
        fields = [
            "id",
            "title",
            "description",
            "priority",
            "due_date",
            "position",
            "is_archived",
            "created_at",
            "updated_at",
            "assignees",
            "assignee_membership_ids",
            "status_id",
            "status",
            "labels",
            "label_ids",
            "attachment_count",
        ]
        read_only_fields = ["id", "position", "is_archived", "created_at", "updated_at"]

    def get_attachment_count(self, task) -> int:
        """Files in the task's attachment strip.

        Cheapest source first: the detail view prefetches, the listing
        annotates, and anything else pays for a query.
        """
        if (prefetched := getattr(task, "strip_attachments", None)) is not None:
            return len(prefetched)
        if (annotated := getattr(task, "attachment_count", None)) is not None:
            return annotated
        return task.attachments.filter(placement=Attachment.Placement.TASK).count()

    def validate_description(self, value):
        """Check the document's file references without touching storage."""
        return sync_description_attachments(task=self.instance, content=value)


class TaskSerializer(BaseTaskSerializer):
    """Used for listing and creating tasks."""


class TaskDetailSerializer(BaseTaskSerializer):
    """Adds attachments and activity recording to the shared task representation."""

    attachments = AttachmentSerializer(source="strip_attachments", many=True, read_only=True)

    class Meta(BaseTaskSerializer.Meta):
        fields = [*BaseTaskSerializer.Meta.fields, "attachments"]
        read_only_fields = [*BaseTaskSerializer.Meta.read_only_fields, "assignees", "attachments"]

    @staticmethod
    def assignee_snapshot(task):
        return sorted(
            (
                {
                    "membership_id": assignment.membership_id,
                    "user_id": assignment.membership.user_id,
                    "display_name": assignment.membership.user.display_name,
                }
                for assignment in task.assignments.all()
            ),
            key=lambda assignee: assignee["membership_id"],
        )

    @classmethod
    def snapshot(cls, task) -> dict:
        return {
            "title": task.title,
            "description": task.description_content,
            "priority": task.priority,
            "status": task.status.name if task.status else None,
            "labels": sorted(label.name for label in task.labels.all()),
            "assignees": cls.assignee_snapshot(task),
        }

    #: Timeline entry recorded when a snapshot key changes.
    ACTIONS = {
        "title": TaskActivity.Action.TITLE_CHANGED,
        "description": TaskActivity.Action.DESCRIPTION_CHANGED,
        "priority": TaskActivity.Action.PRIORITY_CHANGED,
        "status": TaskActivity.Action.STATUS_CHANGED,
        "labels": TaskActivity.Action.LABELS_CHANGED,
        "assignees": TaskActivity.Action.ASSIGNEES_CHANGED,
    }

    #: Too large for every timeline row; the entry records that the field
    #: changed, not what it became.
    UNLOGGED_VALUES = {"description"}

    #: Relations both the diff and the response body walk.
    TASK_PREFETCHES = (
        "assignments__membership__user",
        "labels",
        STRIP_ATTACHMENT_PREFETCH,
    )

    @transaction.atomic
    def update(self, instance, validated_data):
        previous = self.snapshot(instance)

        assignee_membership_ids = validated_data.pop("assignee_membership_ids", None)
        status_id = validated_data.pop("status_id", serializers.empty)
        label_ids = validated_data.pop("label_ids", serializers.empty)

        task = super().update(instance, validated_data)
        if assignee_membership_ids is not None:
            replace_task_assignments(
                task=task,
                assignee_membership_ids=assignee_membership_ids,
            )
        if status_id is not serializers.empty:
            replace_task_status(task=task, status_id=status_id)
        if label_ids is not serializers.empty:
            replace_task_labels(task=task, label_ids=label_ids)

        # Writing to the relations above emptied their prefetch caches, so
        # without this both the snapshot below and the response body would walk
        # assignment -> membership -> user one row at a time.
        prefetch_related_objects([task], *self.TASK_PREFETCHES)

        current = self.snapshot(task)
        author = self.context["request"].user
        for field, action in self.ACTIONS.items():
            if previous[field] == current[field]:
                continue
            payload = (
                {}
                if field in self.UNLOGGED_VALUES
                else {"from": previous[field], "to": current[field]}
            )
            record_task_activity(task=task, author=author, action=action, payload=payload)
        return task


class TaskActivitySerializer(serializers.ModelSerializer):
    author_id = serializers.IntegerField(source="author.id", read_only=True, allow_null=True)
    author_display_name = serializers.CharField(
        source="author.display_name",
        read_only=True,
        allow_null=True,
    )
    comment_id = serializers.IntegerField(source="comment.id", read_only=True, allow_null=True)
    comment_body = serializers.CharField(source="comment.body", read_only=True, allow_null=True)
    comment_updated_at = serializers.DateTimeField(
        source="comment.updated_at",
        read_only=True,
        allow_null=True,
    )
    comment_mentions = serializers.SerializerMethodField()
    comment_attachments = serializers.SerializerMethodField()
    author_is_member = serializers.BooleanField(read_only=True)

    def get_comment_mentions(self, activity) -> list[dict[str, int | str]]:
        if not hasattr(activity, "comment"):
            return []
        return [
            {"user_id": user.id, "display_name": user.display_name}
            for user in activity.comment.mentions.all()
        ]

    @extend_schema_field(AttachmentSerializer(many=True))
    def get_comment_attachments(self, activity):
        """Files posted with a comment, from the timeline's own prefetch."""
        if not hasattr(activity, "comment"):
            return []
        return AttachmentSerializer(
            activity.comment.attachments.all(),
            many=True,
            context=self.context,
        ).data

    class Meta:
        model = TaskActivity
        fields = [
            "id",
            "action",
            "payload",
            "created_at",
            "author_id",
            "author_display_name",
            "author_is_member",
            "comment_id",
            "comment_body",
            "comment_updated_at",
            "comment_mentions",
            "comment_attachments",
        ]


class TaskCommentSerializer(serializers.ModelSerializer):
    mention_user_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        write_only=True,
    )
    attachment_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        write_only=True,
    )
    attachments = AttachmentSerializer(many=True, read_only=True)
    # A file on its own is a comment.
    body = serializers.CharField(required=False, allow_blank=True, default="")

    class Meta:
        model = TaskComment
        fields = [
            "id",
            "body",
            "updated_at",
            "mention_user_ids",
            "attachment_ids",
            "attachments",
        ]
        read_only_fields = ["id", "updated_at", "attachments"]

    def validate(self, attrs):
        """Require something to actually be posted.

        Stored values fill the gaps, or a partial update touching only one half
        would reject itself.
        """
        body = attrs.get("body", self.instance.body if self.instance else "")
        if "attachment_ids" in attrs:
            has_attachments = bool(attrs["attachment_ids"])
        else:
            has_attachments = bool(self.instance and self.instance.attachments.exists())
        if not body.strip() and not has_attachments:
            raise serializers.ValidationError({"body": "Write a comment or attach a file."})
        return attrs

    @transaction.atomic
    def update(self, instance, validated_data):
        mention_user_ids = validated_data.pop("mention_user_ids", None)
        attachment_ids = validated_data.pop("attachment_ids", None)
        if mention_user_ids is not None:
            mention_user_ids = get_task_comment_mentions(
                task=instance.task,
                mention_user_ids=mention_user_ids,
            )
        comment = super().update(instance, validated_data)
        if mention_user_ids is not None:
            comment.mentions.set(mention_user_ids)
        if attachment_ids is not None:
            replace_comment_attachments(comment=comment, attachment_ids=attachment_ids)
        return comment


class MoveTaskSerializer(serializers.Serializer):
    target_list_id = serializers.IntegerField(min_value=1)
    before_task_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)


class MoveBoardListSerializer(serializers.Serializer):
    before_list_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)
