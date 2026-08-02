"""Shared invitation mechanics: workspace and board invitations are one state
machine over different targets."""

import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


def default_invitation_expiry():
    """Expiry for a freshly sent invitation.

    Module-level, not a lambda, so migrations can reference it by import path.
    """
    return timezone.now() + timedelta(days=settings.INVITATION_EXPIRY_DAYS)


class InvitationStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    REJECTED = "rejected", "Rejected"


class BaseInvitation(models.Model):
    """Fields and rules common to every email-bound invitation."""

    Status = InvitationStatus

    email = models.EmailField()
    status = models.CharField(
        max_length=16,
        choices=InvitationStatus.choices,
        default=InvitationStatus.PENDING,
    )
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(default=default_invitation_expiry)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True
        ordering = ["-sent_at", "-id"]

    def save(self, *args, **kwargs):
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= timezone.now()

    @property
    def is_actionable(self) -> bool:
        """True when this invitation can still be accepted or rejected."""
        return self.status == InvitationStatus.PENDING and not self.is_expired

    def reset_for_resend(self, *, invited_by) -> None:
        """Return an invitation to a freshly-sent pending state.

        The token rotates: the old link was emailed and must stop working.
        """
        self.invited_by = invited_by
        self.status = InvitationStatus.PENDING
        self.token = uuid.uuid4()
        self.sent_at = timezone.now()
        self.expires_at = default_invitation_expiry()
        self.responded_at = None
