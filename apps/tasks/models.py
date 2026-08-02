from decimal import Decimal

from django.conf import settings
from django.db import models

from apps.boards.models import Board, BoardLabel, BoardMembership, BoardStatus
from taskflow.models import CreatedAtModel, TimeStampedModel


# Sparse fractional positions: inserting between two rows is one UPDATE.
POSITION_STEP = Decimal("1000")


class BoardList(CreatedAtModel):
    """An ordered column of tasks within a board."""

    board = models.ForeignKey(Board, on_delete=models.CASCADE, related_name="lists")
    name = models.CharField(max_length=255)
    position = models.DecimalField(
        max_digits=20,
        decimal_places=10,
        default=POSITION_STEP,
    )
    is_archived = models.BooleanField(default=False)

    class Meta:
        ordering = ["position", "created_at"]
        indexes = [
            models.Index(
                fields=["board", "is_archived", "position"],
                name="board_list_ordering_idx",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class Task(TimeStampedModel):
    class Priority(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"
        URGENT = "urgent", "Urgent"

    board_list = models.ForeignKey(BoardList, on_delete=models.CASCADE, related_name="tasks")
    title = models.CharField(max_length=500)
    description_content = models.JSONField(default=dict, blank=True)
    priority = models.CharField(  # noqa: DJ001
        max_length=10,
        choices=Priority,
        null=True,
        blank=True,
    )
    status = models.ForeignKey(
        BoardStatus,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tasks",
    )
    labels = models.ManyToManyField(BoardLabel, blank=True, related_name="tasks")
    position = models.DecimalField(
        max_digits=20,
        decimal_places=10,
        default=POSITION_STEP,
    )
    due_date = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_tasks",
    )
    is_archived = models.BooleanField(default=False)

    class Meta:
        ordering = ["position", "created_at"]
        indexes = [
            models.Index(
                fields=["board_list", "is_archived", "position"],
                name="task_ordering_idx",
            ),
        ]

    def __str__(self) -> str:
        return self.title


class TaskActivity(CreatedAtModel):
    """One entry in a task's timeline. Comments attach to a `comment_added` row."""

    class Action(models.TextChoices):
        CREATED = "created", "Created"
        TITLE_CHANGED = "title_changed", "Title changed"
        DESCRIPTION_CHANGED = "description_changed", "Description changed"
        PRIORITY_CHANGED = "priority_changed", "Priority changed"
        STATUS_CHANGED = "status_changed", "Status changed"
        LABELS_CHANGED = "labels_changed", "Labels changed"
        ASSIGNEES_CHANGED = "assignees_changed", "Assignees changed"
        MOVED = "moved", "Moved"
        COMMENT_ADDED = "comment_added", "Comment added"
        ATTACHMENT_ADDED = "attachment_added", "Attachment added"
        ATTACHMENT_REMOVED = "attachment_removed", "Attachment removed"

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="activity")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="task_activity",
    )
    action = models.CharField(max_length=32, choices=Action)
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["task", "-created_at"], name="task_activity_feed_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.action} on task {self.task_id}"


class TaskComment(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="comments")
    activity = models.OneToOneField(
        TaskActivity,
        on_delete=models.CASCADE,
        related_name="comment",
    )
    body = models.TextField()
    mentions = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="mentioned_in_task_comments",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"Comment on task {self.task_id}"


class TaskAssignment(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="assignments")
    membership = models.ForeignKey(
        BoardMembership,
        on_delete=models.CASCADE,
        related_name="task_assignments",
    )
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["task", "membership"],
                name="unique_task_assignment",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.membership.user} assigned to {self.task}"
