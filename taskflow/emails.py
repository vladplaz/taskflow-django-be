"""Invitation email delivery, over whatever ``EMAIL_BACKEND`` is configured."""

from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string


@dataclass(frozen=True)
class InvitationEmail:
    """Everything an invitation email needs, independent of which app sent it."""

    recipient_email: str
    workspace_name: str
    inviter_name: str
    invite_url: str
    expires_at: datetime
    board_name: str | None = None

    @property
    def is_board_invitation(self) -> bool:
        return self.board_name is not None


def _frontend_url(path: str) -> str:
    return f"{settings.FRONTEND_BASE_URL.rstrip('/')}/{path.lstrip('/')}"


def workspace_invitation_url(token) -> str:
    return _frontend_url(f"invite/{token}")


def board_invitation_url(token) -> str:
    return _frontend_url(f"invite/board/{token}")


def send_invitation_email(invitation: InvitationEmail) -> None:
    """Render and deliver one invitation using the configured email backend."""
    if invitation.is_board_invitation:
        subject = f"{invitation.inviter_name} invited you to the {invitation.board_name} board"
        template = "emails/board_invitation.txt"
    else:
        subject = (
            f"{invitation.inviter_name} invited you to the {invitation.workspace_name} workspace"
        )
        template = "emails/workspace_invitation.txt"

    send_mail(
        subject=subject,
        message=render_to_string(template, {"invitation": invitation}),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[invitation.recipient_email],
    )
