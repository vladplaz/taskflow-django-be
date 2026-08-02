from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.boards.models import BoardMembership
from apps.tasks.models import TaskComment
from taskflow.emails import (
    InvitationEmail,
    send_invitation_email,
    workspace_invitation_url,
)

from .models import Workspace, WorkspaceInvitation, WorkspaceMembership


WORKSPACE_ADMIN_ROLES = [
    WorkspaceMembership.Role.OWNER,
    WorkspaceMembership.Role.ADMIN,
]


@transaction.atomic
def create_workspace(*, name: str, description: str, creator) -> Workspace:
    """Create a workspace and its initial owner membership as one operation."""
    workspace = Workspace.objects.create(
        name=name,
        description=description,
        created_by=creator,
    )
    WorkspaceMembership.objects.create(
        workspace=workspace,
        user=creator,
        role=WorkspaceMembership.Role.OWNER,
    )
    return workspace


@transaction.atomic
def delete_workspace(*, workspace: Workspace) -> None:
    """Permanently remove a workspace and everything cascading from it."""
    workspace.delete()


def normalize_invitation_email(email: str) -> str:
    return get_user_model().objects.normalize_email(email).strip().lower()


def send_workspace_invitation_email(invitation: WorkspaceInvitation) -> None:
    send_invitation_email(
        InvitationEmail(
            recipient_email=invitation.email,
            workspace_name=invitation.workspace.name,
            inviter_name=invitation.invited_by.display_name or invitation.invited_by.email,
            invite_url=workspace_invitation_url(invitation.token),
            expires_at=invitation.expires_at,
        )
    )


@transaction.atomic
def create_workspace_invitation(
    *,
    workspace: Workspace,
    email: str,
    invited_by,
) -> WorkspaceInvitation:
    normalized_email = normalize_invitation_email(email)
    if WorkspaceMembership.objects.filter(
        workspace=workspace,
        user__email=normalized_email,
    ).exists():
        raise ValidationError({"email": "This user is already a member of the workspace."})

    existing = (
        WorkspaceInvitation.objects.select_for_update()
        .filter(workspace=workspace, email=normalized_email)
        .first()
    )
    if existing and existing.is_actionable:
        raise ValidationError({"email": "An invitation is already pending for this email."})
    if existing and existing.status == WorkspaceInvitation.Status.REJECTED:
        raise ValidationError(
            {"email": "This invitation was rejected. Use resend to invite them again."}
        )

    invited_user = get_user_model().objects.filter(email=normalized_email).first()
    if existing:
        # Reuse the row so the unique (workspace, email) constraint still holds.
        existing.reset_for_resend(invited_by=invited_by)
        existing.invited_user = invited_user
        existing.save()
        invitation = existing
    else:
        # `select_for_update()` locked nothing -- there was no row -- so two
        # concurrent invitations both reach here. The unique constraint
        # decides; the savepoint keeps the loser's IntegrityError from
        # poisoning the enclosing transaction.
        try:
            with transaction.atomic():
                invitation = WorkspaceInvitation.objects.create(
                    workspace=workspace,
                    email=normalized_email,
                    invited_user=invited_user,
                    invited_by=invited_by,
                )
        except IntegrityError as error:
            raise ValidationError(
                {"email": "An invitation is already pending for this email."}
            ) from error
    transaction.on_commit(lambda: send_workspace_invitation_email(invitation))
    return invitation


@transaction.atomic
def resend_workspace_invitation(
    *,
    invitation: WorkspaceInvitation,
    invited_by,
) -> WorkspaceInvitation:
    invitation = (
        WorkspaceInvitation.objects.select_for_update()
        .select_related("workspace")
        .get(pk=invitation.pk)
    )
    if invitation.is_actionable:
        raise ValidationError({"detail": "This invitation is still pending."})
    if invitation.status == WorkspaceInvitation.Status.ACCEPTED:
        raise ValidationError({"detail": "This invitation was already accepted."})
    if WorkspaceMembership.objects.filter(
        workspace=invitation.workspace,
        user__email=invitation.email,
    ).exists():
        raise ValidationError({"detail": "This user is already a member of the workspace."})

    invitation.invited_user = get_user_model().objects.filter(email=invitation.email).first()
    invitation.reset_for_resend(invited_by=invited_by)
    invitation.save()
    transaction.on_commit(lambda: send_workspace_invitation_email(invitation))
    return invitation


@transaction.atomic
def respond_to_workspace_invitation(
    *,
    invitation: WorkspaceInvitation,
    user,
    accept: bool,
) -> WorkspaceInvitation:
    invitation = (
        WorkspaceInvitation.objects.select_for_update()
        .select_related("workspace")
        .get(pk=invitation.pk)
    )
    if invitation.status != WorkspaceInvitation.Status.PENDING:
        raise ValidationError({"detail": "This invitation is no longer pending."})
    if invitation.is_expired:
        raise ValidationError({"detail": "This invitation has expired. Ask for a new one."})
    if invitation.email.casefold() != user.email.casefold():
        raise ValidationError({"detail": "This invitation belongs to a different email address."})

    invitation.invited_user = user
    invitation.responded_at = timezone.now()
    if accept:
        WorkspaceMembership.objects.get_or_create(
            workspace=invitation.workspace,
            user=user,
            defaults={"role": WorkspaceMembership.Role.MEMBER},
        )
        invitation.status = WorkspaceInvitation.Status.ACCEPTED
    else:
        invitation.status = WorkspaceInvitation.Status.REJECTED
    invitation.save(update_fields=["invited_user", "responded_at", "status"])
    return invitation


def _detach_from_workspace(membership: WorkspaceMembership) -> None:
    """Drop a member's mentions and board memberships, then the membership."""
    TaskComment.mentions.through.objects.filter(
        taskcomment__task__board_list__board__workspace_id=membership.workspace_id,
        user_id=membership.user_id,
    ).delete()
    BoardMembership.objects.filter(
        board__workspace_id=membership.workspace_id,
        user_id=membership.user_id,
    ).delete()
    membership.delete()


@transaction.atomic
def remove_workspace_member(*, membership: WorkspaceMembership, acting_user) -> None:
    """Remove a member and every board membership they held in this workspace."""
    membership = (
        WorkspaceMembership.objects.select_for_update()
        .select_related("workspace", "user")
        .get(pk=membership.pk)
    )
    if membership.role == WorkspaceMembership.Role.OWNER:
        raise ValidationError({"detail": "The workspace owner cannot be removed."})
    if membership.user_id == acting_user.id:
        raise ValidationError({"detail": "You cannot remove yourself from this workspace."})

    _detach_from_workspace(membership)


@transaction.atomic
def leave_workspace(*, workspace: Workspace, user) -> None:
    """Give up your own workspace membership.

    Separate from `remove_workspace_member`, which refuses self-removal to
    catch the misclick. The owner still cannot leave.
    """
    membership = get_object_or_404(
        WorkspaceMembership.objects.select_for_update(),
        workspace=workspace,
        user=user,
    )
    if membership.role == WorkspaceMembership.Role.OWNER:
        raise ValidationError({"detail": "The owner cannot leave; delete the workspace instead."})
    _detach_from_workspace(membership)


@transaction.atomic
def update_workspace_member_role(
    *,
    membership: WorkspaceMembership,
    role: str,
    acting_user,
) -> WorkspaceMembership:
    membership = WorkspaceMembership.objects.select_for_update().get(pk=membership.pk)
    if membership.role == WorkspaceMembership.Role.OWNER:
        raise ValidationError({"detail": "The workspace owner's role cannot be changed."})
    if membership.user_id == acting_user.id:
        raise ValidationError({"detail": "You cannot change your own workspace role."})
    if role == WorkspaceMembership.Role.OWNER:
        raise ValidationError({"detail": "A workspace can only have one owner."})
    membership.role = role
    membership.save(update_fields=["role"])
    return membership
