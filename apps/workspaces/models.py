from django.conf import settings
from django.db import models

from taskflow.invitations import BaseInvitation
from taskflow.models import CreatedAtModel, TimeStampedModel


class Workspace(TimeStampedModel):
    """A tenant that contains boards, cards, and future project data."""

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_workspaces",
    )

    class Meta:
        ordering = ["name", "id"]

    def __str__(self) -> str:
        return self.name


class WorkspaceMembership(CreatedAtModel):
    """Connects a user to a workspace and records their authorization role."""

    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        MEMBER = "member", "Member"

    #: Roles that carry implicit administration over every board in the workspace.
    ADMIN_ROLES = [Role.OWNER, Role.ADMIN]
    #: Assignable after creation; ownership is not transferable through the API.
    ASSIGNABLE_ROLES = [Role.ADMIN, Role.MEMBER]

    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="workspace_memberships",
    )
    role = models.CharField(max_length=16, choices=Role.choices)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "user"],
                name="unique_workspace_membership",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "role"], name="ws_membership_user_role_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.user} in {self.workspace} ({self.role})"


# Module-level aliases: the OpenAPI generator names enums by import path and
# cannot resolve an attribute nested inside a class.
WORKSPACE_ROLE_CHOICES = WorkspaceMembership.Role.choices
WORKSPACE_ASSIGNABLE_ROLE_CHOICES = [
    (role.value, role.label) for role in WorkspaceMembership.ASSIGNABLE_ROLES
]


class WorkspaceInvitation(BaseInvitation):
    """An email-bound invitation to join a workspace."""

    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name="invitations",
    )
    invited_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="workspace_invitations",
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="sent_workspace_invitations",
    )

    class Meta(BaseInvitation.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "email"],
                name="unique_workspace_invitation_email",
            ),
        ]
        indexes = [
            models.Index(fields=["email", "status"], name="ws_invite_email_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.email} invited to {self.workspace} ({self.status})"
