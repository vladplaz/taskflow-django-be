from django.contrib.auth import get_user_model
from rest_framework import serializers

from taskflow.emails import workspace_invitation_url

from .models import (
    WORKSPACE_ASSIGNABLE_ROLE_CHOICES,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMembership,
)


class WorkspaceSerializer(serializers.ModelSerializer):
    # Annotated by the view with a Subquery; see WorkspaceListCreateView.
    membership_role = serializers.CharField(read_only=True)

    class Meta:
        model = Workspace
        fields = [
            "id",
            "name",
            "description",
            "membership_role",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "membership_role", "created_at", "updated_at"]


class WorkspaceMemberSerializer(serializers.ModelSerializer):
    user_id = serializers.IntegerField(source="user.id", read_only=True)
    display_name = serializers.CharField(source="user.display_name", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = WorkspaceMembership
        fields = ["id", "user_id", "display_name", "email", "role", "created_at"]
        read_only_fields = ["id", "user_id", "display_name", "email", "created_at"]


class WorkspaceMemberRoleSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=WORKSPACE_ASSIGNABLE_ROLE_CHOICES)


class WorkspaceInvitationSerializer(serializers.ModelSerializer):
    workspace_id = serializers.IntegerField(source="workspace.id", read_only=True)
    workspace_name = serializers.CharField(source="workspace.name", read_only=True)
    invited_by_name = serializers.CharField(source="invited_by.display_name", read_only=True)
    invite_url = serializers.SerializerMethodField()
    is_expired = serializers.BooleanField(read_only=True)

    def get_invite_url(self, invitation) -> str:
        return workspace_invitation_url(invitation.token)

    class Meta:
        model = WorkspaceInvitation
        fields = [
            "id",
            "workspace_id",
            "workspace_name",
            "email",
            "status",
            "is_expired",
            "invited_by_name",
            "invite_url",
            "created_at",
            "sent_at",
            "expires_at",
            "responded_at",
        ]
        read_only_fields = fields


class CreateWorkspaceInvitationSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PublicWorkspaceInvitationSerializer(serializers.ModelSerializer):
    """Unauthenticated view of an invitation link; see the board equivalent."""

    workspace_name = serializers.CharField(source="workspace.name", read_only=True)
    recipient_registered = serializers.SerializerMethodField()

    def get_recipient_registered(self, invitation) -> bool:
        return get_user_model().objects.filter(email=invitation.email).exists()

    class Meta:
        model = WorkspaceInvitation
        fields = [
            "email",
            "workspace_name",
            "status",
            "expires_at",
            "recipient_registered",
        ]
