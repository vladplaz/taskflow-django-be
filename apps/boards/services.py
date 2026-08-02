from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.tasks.models import TaskComment
from taskflow.emails import InvitationEmail, board_invitation_url, send_invitation_email

from .models import Board, BoardInvitation, BoardLabel, BoardMembership, BoardStatus


DEFAULT_STATUS_NAMES = ["To do", "In progress", "In review", "Done"]
DEFAULT_LABELS = [
    ("Feature", "#2563EB"),
    ("Bug", "#DC2626"),
    ("Improvement", "#16A34A"),
]
POSITION_STEP = 1000


@transaction.atomic
def create_board(
    *,
    workspace,
    creator,
    name: str,
    description: str,
    visibility: str = Board.Visibility.PRIVATE,
) -> Board:
    board = Board.objects.create(
        workspace=workspace,
        created_by=creator,
        name=name,
        description=description,
        visibility=visibility,
    )
    BoardMembership.objects.create(
        board=board,
        user=creator,
        role=BoardMembership.Role.ADMIN,
    )
    BoardStatus.objects.bulk_create(
        [
            BoardStatus(board=board, name=status_name, position=index * POSITION_STEP)
            for index, status_name in enumerate(DEFAULT_STATUS_NAMES, start=1)
        ]
    )
    BoardLabel.objects.bulk_create(
        [
            BoardLabel(board=board, name=label_name, color=color, position=index * POSITION_STEP)
            for index, (label_name, color) in enumerate(DEFAULT_LABELS, start=1)
        ]
    )
    return board


@transaction.atomic
def archive_board(*, board: Board) -> Board:
    """Soft-delete a board, keeping its tasks, activity, and comments."""
    board.is_archived = True
    board.save(update_fields=["is_archived", "updated_at"])
    return board


@transaction.atomic
def restore_board(*, board: Board) -> Board:
    """Bring an archived board back."""
    board.is_archived = False
    board.save(update_fields=["is_archived", "updated_at"])
    return board


def next_status_position(board: Board) -> int:
    return (board.statuses.aggregate(last=Max("position"))["last"] or 0) + POSITION_STEP


def next_label_position(board: Board) -> int:
    return (board.labels.aggregate(last=Max("position"))["last"] or 0) + POSITION_STEP


def normalize_board_invitation_email(email: str) -> str:
    return get_user_model().objects.normalize_email(email).strip().lower()


def send_board_invitation_email(invitation: BoardInvitation) -> None:
    send_invitation_email(
        InvitationEmail(
            recipient_email=invitation.email,
            board_name=invitation.board.name,
            workspace_name=invitation.board.workspace.name,
            inviter_name=invitation.invited_by.display_name or invitation.invited_by.email,
            invite_url=board_invitation_url(invitation.token),
            expires_at=invitation.expires_at,
        )
    )


@transaction.atomic
def create_board_invitation(
    *,
    board: Board,
    email: str,
    role: str,
    invited_by,
) -> BoardInvitation:
    normalized_email = normalize_board_invitation_email(email)
    if BoardMembership.objects.filter(board=board, user__email=normalized_email).exists():
        raise ValidationError({"email": "This user is already a member of the board."})

    existing = (
        BoardInvitation.objects.select_for_update()
        .filter(board=board, email=normalized_email)
        .first()
    )
    if existing and existing.is_actionable:
        raise ValidationError({"email": "An invitation is already pending for this email."})
    if existing and existing.status == BoardInvitation.Status.REJECTED:
        raise ValidationError(
            {"email": "This invitation was rejected. Use resend to invite them again."}
        )

    invited_user = get_user_model().objects.filter(email=normalized_email).first()
    if existing:
        # Reuse the row so the unique (board, email) constraint still holds.
        existing.reset_for_resend(invited_by=invited_by)
        existing.invited_user = invited_user
        existing.role = role
        existing.save()
        invitation = existing
    else:
        # `select_for_update()` locked nothing -- there was no row -- so two
        # concurrent invitations both reach here. The unique constraint
        # decides; the savepoint keeps the loser's IntegrityError from
        # poisoning the enclosing transaction.
        try:
            with transaction.atomic():
                invitation = BoardInvitation.objects.create(
                    board=board,
                    email=normalized_email,
                    invited_user=invited_user,
                    invited_by=invited_by,
                    role=role,
                )
        except IntegrityError as error:
            raise ValidationError(
                {"email": "An invitation is already pending for this email."}
            ) from error
    transaction.on_commit(lambda: send_board_invitation_email(invitation))
    return invitation


@transaction.atomic
def resend_board_invitation(*, invitation: BoardInvitation, invited_by) -> BoardInvitation:
    invitation = (
        BoardInvitation.objects.select_for_update()
        .select_related("board__workspace")
        .get(pk=invitation.pk)
    )
    if invitation.is_actionable:
        raise ValidationError({"detail": "This invitation is still pending."})
    if invitation.status == BoardInvitation.Status.ACCEPTED:
        raise ValidationError({"detail": "This invitation was already accepted."})
    if BoardMembership.objects.filter(
        board=invitation.board,
        user__email=invitation.email,
    ).exists():
        raise ValidationError({"detail": "This user is already a member of the board."})

    invitation.invited_user = get_user_model().objects.filter(email=invitation.email).first()
    invitation.reset_for_resend(invited_by=invited_by)
    invitation.save()
    transaction.on_commit(lambda: send_board_invitation_email(invitation))
    return invitation


@transaction.atomic
def respond_to_board_invitation(
    *,
    invitation: BoardInvitation,
    user,
    accept: bool,
) -> BoardInvitation:
    invitation = (
        BoardInvitation.objects.select_for_update().select_related("board").get(pk=invitation.pk)
    )
    if invitation.status != BoardInvitation.Status.PENDING:
        raise ValidationError({"detail": "This invitation is no longer pending."})
    if invitation.is_expired:
        raise ValidationError({"detail": "This invitation has expired. Ask for a new one."})
    if invitation.email.casefold() != user.email.casefold():
        raise ValidationError({"detail": "This invitation belongs to a different email address."})

    invitation.invited_user = user
    invitation.responded_at = timezone.now()
    if accept:
        BoardMembership.objects.update_or_create(
            board=invitation.board,
            user=user,
            defaults={"role": invitation.role},
        )
        invitation.status = BoardInvitation.Status.ACCEPTED
    else:
        invitation.status = BoardInvitation.Status.REJECTED
    invitation.save(update_fields=["invited_user", "responded_at", "status"])
    return invitation


def clear_mentions_on_board(*, board: Board, user) -> None:
    """Drop a departing user's comment mentions in one DELETE."""
    TaskComment.mentions.through.objects.filter(
        taskcomment__task__board_list__board=board,
        user=user,
    ).delete()


def assert_not_last_board_admin(*, membership: BoardMembership) -> None:
    if membership.role != BoardMembership.Role.ADMIN:
        return
    another_admin_exists = (
        BoardMembership.objects.filter(
            board_id=membership.board_id,
            role=BoardMembership.Role.ADMIN,
        )
        .exclude(pk=membership.pk)
        .exists()
    )
    if not another_admin_exists:
        raise ValidationError(
            {"detail": "Promote another board admin before changing this membership."}
        )


@transaction.atomic
def remove_board_member(*, membership: BoardMembership, acting_user) -> None:
    membership = (
        BoardMembership.objects.select_for_update().select_related("board").get(pk=membership.pk)
    )
    if membership.user_id == acting_user.id:
        raise ValidationError({"detail": "You cannot remove yourself from this board."})
    assert_not_last_board_admin(membership=membership)
    clear_mentions_on_board(board=membership.board, user=membership.user)
    membership.delete()


@transaction.atomic
def leave_board(*, board: Board, user) -> None:
    """Give up your own board membership.

    Separate from `remove_board_member`, which refuses self-removal to catch
    the misclick. The last-admin rule still applies.
    """
    membership = get_object_or_404(
        BoardMembership.objects.select_for_update().select_related("board"),
        board=board,
        user=user,
    )
    assert_not_last_board_admin(membership=membership)
    clear_mentions_on_board(board=board, user=user)
    membership.delete()


@transaction.atomic
def update_board_member_role(
    *,
    membership: BoardMembership,
    role: str,
    acting_user,
) -> BoardMembership:
    membership = BoardMembership.objects.select_for_update().get(pk=membership.pk)
    if membership.user_id == acting_user.id:
        raise ValidationError({"detail": "You cannot change your own board role."})
    if role != BoardMembership.Role.ADMIN:
        assert_not_last_board_admin(membership=membership)
    membership.role = role
    membership.save(update_fields=["role"])
    return membership
