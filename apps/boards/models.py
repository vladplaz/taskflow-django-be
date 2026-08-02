from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models

from apps.workspaces.models import Workspace
from taskflow.invitations import BaseInvitation
from taskflow.models import CreatedAtModel, TimeStampedModel


class Board(TimeStampedModel):
    """A project board that belongs to one workspace."""

    class Visibility(models.TextChoices):
        PRIVATE = "private", "Private"
        WORKSPACE = "workspace", "Workspace"

    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name="boards",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    visibility = models.CharField(
        max_length=16,
        choices=Visibility.choices,
        default=Visibility.PRIVATE,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_boards",
    )
    is_archived = models.BooleanField(default=False)

    class Meta:
        ordering = ["-updated_at", "-id"]
        indexes = [
            models.Index(
                fields=["workspace", "is_archived"],
                name="board_workspace_archived_idx",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class BoardMembership(CreatedAtModel):
    """A user's explicit collaboration role on one board."""

    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        MEMBER = "member", "Member"
        VIEWER = "viewer", "Viewer"

    #: Roles allowed to change board content (lists, tasks, comments).
    EDITOR_ROLES = [Role.ADMIN, Role.MEMBER]
    #: Roles an invitation may grant. Admin is only reachable by promotion.
    INVITABLE_ROLES = [Role.MEMBER, Role.VIEWER]

    board = models.ForeignKey(Board, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="board_memberships",
    )
    role = models.CharField(max_length=16, choices=Role.choices)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["board", "user"],
                name="unique_board_membership",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "role"], name="board_membership_user_role_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.user} on {self.board} ({self.role})"


# Module-level aliases: the OpenAPI generator names enums by import path and
# cannot resolve an attribute nested inside a class.
BOARD_ROLE_CHOICES = BoardMembership.Role.choices
BOARD_INVITABLE_ROLE_CHOICES = [
    (role.value, role.label) for role in BoardMembership.INVITABLE_ROLES
]


class BoardInvitation(BaseInvitation):
    """An email-bound invitation to collaborate on one board."""

    board = models.ForeignKey(Board, on_delete=models.CASCADE, related_name="invitations")
    invited_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="board_invitations",
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="sent_board_invitations",
    )
    role = models.CharField(
        max_length=16,
        choices=BoardMembership.Role.choices,
        default=BoardMembership.Role.MEMBER,
    )

    class Meta(BaseInvitation.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["board", "email"],
                name="unique_board_invitation_email",
            ),
        ]
        indexes = [
            models.Index(
                fields=["email", "status"],
                name="board_invite_email_status_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.email} invited to {self.board} ({self.status})"


class BoardStatus(models.Model):
    """A board-owned, optional classification for tasks."""

    board = models.ForeignKey(Board, on_delete=models.CASCADE, related_name="statuses")
    name = models.CharField(max_length=80)
    position = models.PositiveIntegerField()

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["board", "name"],
                name="unique_board_status_name",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class BoardLabel(models.Model):
    """A colored, board-owned label that can be applied to many tasks."""

    board = models.ForeignKey(Board, on_delete=models.CASCADE, related_name="labels")
    name = models.CharField(max_length=80)
    color = models.CharField(
        max_length=7,
        validators=[
            RegexValidator(r"^#[0-9A-Fa-f]{6}$", "Use a hex color such as #7C3AED."),
        ],
    )
    position = models.PositiveIntegerField()

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["board", "name"],
                name="unique_board_label_name",
            ),
        ]

    def __str__(self) -> str:
        return self.name
