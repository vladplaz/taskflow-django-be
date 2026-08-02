from django.contrib.auth import get_user_model
from rest_framework import serializers

from taskflow.emails import board_invitation_url

from .access import access_role
from .models import (
    BOARD_INVITABLE_ROLE_CHOICES,
    Board,
    BoardInvitation,
    BoardLabel,
    BoardMembership,
    BoardStatus,
)


_UNANNOTATED = object()


class BoardSerializer(serializers.ModelSerializer):
    workspace_id = serializers.IntegerField(source="workspace.id", read_only=True)
    workspace_name = serializers.CharField(source="workspace.name", read_only=True)
    access_role = serializers.SerializerMethodField()
    can_edit = serializers.SerializerMethodField()
    can_admin = serializers.SerializerMethodField()

    class Meta:
        model = Board
        fields = [
            "id",
            "workspace_id",
            "workspace_name",
            "name",
            "description",
            "visibility",
            "access_role",
            "can_edit",
            "can_admin",
            "is_archived",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "workspace_id",
            "workspace_name",
            "access_role",
            "can_edit",
            "can_admin",
            "is_archived",
            "created_at",
            "updated_at",
        ]

    def _effective_role(self, board):
        """Resolve the requesting user's role once per board.

        Listing endpoints must call `access.with_viewer_roles`, or the
        fallback below costs two queries per row.
        """
        cached = getattr(board, "_effective_access_role", _UNANNOTATED)
        if cached is not _UNANNOTATED:
            return cached

        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            role = None
        else:
            user = request.user
            if hasattr(board, "viewer_workspace_role"):
                workspace_role = board.viewer_workspace_role
                board_role = board.viewer_board_role
            else:
                workspace_role = (
                    board.workspace.memberships.filter(user=user)
                    .values_list("role", flat=True)
                    .first()
                )
                board_role = (
                    board.memberships.filter(user=user).values_list("role", flat=True).first()
                )
            role = access_role(
                workspace_role=workspace_role,
                board_role=board_role,
                visibility=board.visibility,
            )

        board._effective_access_role = role
        return role

    def get_access_role(self, board) -> str | None:
        return self._effective_role(board)

    def get_can_edit(self, board) -> bool:
        return self._effective_role(board) in BoardMembership.EDITOR_ROLES

    def get_can_admin(self, board) -> bool:
        return self._effective_role(board) == BoardMembership.Role.ADMIN


class BoardMembershipSerializer(serializers.ModelSerializer):
    user_id = serializers.IntegerField(source="user.id", read_only=True)
    display_name = serializers.CharField(source="user.display_name", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    # Annotated by the view with Exists(); see BoardMemberListView.
    is_workspace_member = serializers.BooleanField(read_only=True)

    class Meta:
        model = BoardMembership
        fields = [
            "id",
            "user_id",
            "display_name",
            "email",
            "role",
            "is_workspace_member",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "user_id",
            "display_name",
            "email",
            "is_workspace_member",
            "created_at",
        ]


class BoardMemberRoleSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=BoardMembership.Role.choices)


class BoardInvitationSerializer(serializers.ModelSerializer):
    board_id = serializers.IntegerField(source="board.id", read_only=True)
    board_name = serializers.CharField(source="board.name", read_only=True)
    workspace_id = serializers.IntegerField(source="board.workspace.id", read_only=True)
    workspace_name = serializers.CharField(source="board.workspace.name", read_only=True)
    invited_by_name = serializers.CharField(source="invited_by.display_name", read_only=True)
    invite_url = serializers.SerializerMethodField()
    is_expired = serializers.BooleanField(read_only=True)

    def get_invite_url(self, invitation) -> str:
        return board_invitation_url(invitation.token)

    class Meta:
        model = BoardInvitation
        fields = [
            "id",
            "board_id",
            "board_name",
            "workspace_id",
            "workspace_name",
            "email",
            "role",
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


class CreateBoardInvitationSerializer(serializers.Serializer):
    email = serializers.EmailField()
    role = serializers.ChoiceField(
        choices=BOARD_INVITABLE_ROLE_CHOICES,
        default=BoardMembership.Role.MEMBER,
    )


class PublicBoardInvitationSerializer(serializers.ModelSerializer):
    """Unauthenticated view of an invitation link.

    Readable by anyone holding the token, so it exposes only what the
    acceptance screen needs.
    """

    board_name = serializers.CharField(source="board.name", read_only=True)
    workspace_name = serializers.CharField(source="board.workspace.name", read_only=True)
    recipient_registered = serializers.SerializerMethodField()

    def get_recipient_registered(self, invitation) -> bool:
        return get_user_model().objects.filter(email=invitation.email).exists()

    class Meta:
        model = BoardInvitation
        fields = [
            "email",
            "board_name",
            "workspace_name",
            "role",
            "status",
            "expires_at",
            "recipient_registered",
        ]


class BoardScopedNameSerializer(serializers.ModelSerializer):
    """Base for board-owned records whose name is unique within the board.

    `board` is not a serializer field, so DRF cannot build the usual
    UniqueTogetherValidator and a duplicate would surface as a 500.
    """

    def validate_name(self, name: str) -> str:
        board = self.context["board"]
        duplicates = self.Meta.model.objects.filter(board=board, name=name)
        if self.instance is not None:
            duplicates = duplicates.exclude(pk=self.instance.pk)
        if duplicates.exists():
            label = self.Meta.model._meta.verbose_name
            raise serializers.ValidationError(f"This board already has a {label} with that name.")
        return name


class BoardStatusSerializer(BoardScopedNameSerializer):
    class Meta:
        model = BoardStatus
        fields = ["id", "name", "position"]
        read_only_fields = ["id", "position"]


class BoardLabelSerializer(BoardScopedNameSerializer):
    class Meta:
        model = BoardLabel
        fields = ["id", "name", "color", "position"]
        read_only_fields = ["id", "position"]
