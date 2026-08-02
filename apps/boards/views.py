from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import generics, permissions, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.workspaces.models import Workspace, WorkspaceMembership
from taskflow.views import ARCHIVED_PARAMETER, ArchiveFilterMixin

from .access import (
    WORKSPACE_ADMIN_ROLES,
    administered_boards_for,
    visible_boards_for,
    with_viewer_roles,
)
from .models import Board, BoardInvitation, BoardLabel, BoardMembership, BoardStatus
from .serializers import (
    BoardInvitationSerializer,
    BoardLabelSerializer,
    BoardMemberRoleSerializer,
    BoardMembershipSerializer,
    BoardSerializer,
    BoardStatusSerializer,
    CreateBoardInvitationSerializer,
    PublicBoardInvitationSerializer,
)
from .services import (
    archive_board,
    create_board,
    create_board_invitation,
    leave_board,
    next_label_position,
    next_status_position,
    remove_board_member,
    resend_board_invitation,
    respond_to_board_invitation,
    restore_board,
    update_board_member_role,
)


class BoardArchiveFilterMixin(ArchiveFilterMixin):
    """Turns `?archived=true` into the board listing it asks for."""

    def board_listing(self):
        archived = self.wants_archived()
        queryset = visible_boards_for(self.request.user, include_archived=archived)
        if archived:
            queryset = queryset.filter(is_archived=True)
        return queryset


@extend_schema_view(get=extend_schema(parameters=[ARCHIVED_PARAMETER]))
class MyBoardListView(BoardArchiveFilterMixin, generics.ListAPIView):
    serializer_class = BoardSerializer

    def get_queryset(self):
        return with_viewer_roles(
            self.board_listing().select_related("workspace"),
            self.request.user,
        )


@extend_schema_view(get=extend_schema(parameters=[ARCHIVED_PARAMETER]))
class WorkspaceBoardListCreateView(BoardArchiveFilterMixin, generics.ListCreateAPIView):
    serializer_class = BoardSerializer

    def get_admin_workspace(self) -> Workspace:
        return get_object_or_404(
            Workspace.objects.filter(
                memberships__user=self.request.user,
                memberships__role__in=WORKSPACE_ADMIN_ROLES,
            ),
            pk=self.kwargs["workspace_id"],
        )

    def get_queryset(self):
        return with_viewer_roles(
            self.board_listing()
            .filter(workspace_id=self.kwargs["workspace_id"])
            .select_related("workspace"),
            self.request.user,
        )

    def perform_create(self, serializer):
        workspace = self.get_admin_workspace()
        board = create_board(
            workspace=workspace,
            creator=self.request.user,
            name=serializer.validated_data["name"],
            description=serializer.validated_data.get("description", ""),
            visibility=serializer.validated_data.get("visibility", Board.Visibility.PRIVATE),
        )
        # The serializer expects these annotations; the creator is always a
        # board admin.
        board.viewer_workspace_role = (
            workspace.memberships.filter(user=self.request.user)
            .values_list("role", flat=True)
            .first()
        )
        board.viewer_board_role = BoardMembership.Role.ADMIN
        serializer.instance = board


class BoardDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = BoardSerializer

    def get_queryset(self):
        queryset = (
            visible_boards_for(self.request.user)
            if self.request.method in permissions.SAFE_METHODS
            else administered_boards_for(self.request.user)
        )
        return with_viewer_roles(
            queryset.filter(workspace_id=self.kwargs["workspace_id"]).select_related("workspace"),
            self.request.user,
        )

    def perform_destroy(self, instance):
        archive_board(board=instance)


@extend_schema(request=None, responses={200: BoardSerializer})
class BoardRestoreView(APIView):
    """Bring an archived board back."""

    def post(self, request, workspace_id: int, board_id: int):
        board = get_object_or_404(
            administered_boards_for(request.user, include_archived=True).filter(is_archived=True),
            pk=board_id,
            workspace_id=workspace_id,
        )
        restored = restore_board(board=board)
        serializer = BoardSerializer(restored, context={"request": request})
        return Response(serializer.data)


class BoardOwnedResourceMixin:
    """Resolves the board once per request for board-owned collections."""

    def get_board(self) -> Board:
        if not hasattr(self, "_board"):
            queryset = (
                visible_boards_for(self.request.user)
                if self.request.method in permissions.SAFE_METHODS
                else administered_boards_for(self.request.user)
            )
            self._board = get_object_or_404(
                queryset,
                pk=self.kwargs["board_id"],
                workspace_id=self.kwargs["workspace_id"],
            )
        return self._board

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "board": self.get_board()}

    def create_board_owned(self, serializer, *, next_position):
        """Create a board-owned row, serialising position allocation.

        The unique (board, name) constraint is the real guarantee; this turns
        the losing side of a race into a 400 rather than a 500.
        """
        try:
            with transaction.atomic():
                board = Board.objects.select_for_update().get(pk=self.get_board().pk)
                serializer.save(board=board, position=next_position(board))
        except IntegrityError as error:
            raise serializers.ValidationError(
                {"name": "This board already has an entry with that name."}
            ) from error


class BoardStatusListCreateView(BoardOwnedResourceMixin, generics.ListCreateAPIView):
    serializer_class = BoardStatusSerializer

    def get_queryset(self):
        return BoardStatus.objects.filter(board=self.get_board())

    def perform_create(self, serializer):
        self.create_board_owned(serializer, next_position=next_status_position)


class BoardStatusDetailView(BoardOwnedResourceMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = BoardStatusSerializer

    def get_queryset(self):
        return BoardStatus.objects.filter(board=self.get_board())


class BoardLabelListCreateView(BoardOwnedResourceMixin, generics.ListCreateAPIView):
    serializer_class = BoardLabelSerializer

    def get_queryset(self):
        return BoardLabel.objects.filter(board=self.get_board())

    def perform_create(self, serializer):
        self.create_board_owned(serializer, next_position=next_label_position)


class BoardLabelDetailView(BoardOwnedResourceMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = BoardLabelSerializer

    def get_queryset(self):
        return BoardLabel.objects.filter(board=self.get_board())


class BoardMemberListView(generics.ListAPIView):
    serializer_class = BoardMembershipSerializer

    def get_queryset(self):
        board = get_object_or_404(
            visible_boards_for(self.request.user),
            pk=self.kwargs["board_id"],
            workspace_id=self.kwargs["workspace_id"],
        )
        workspace_membership = WorkspaceMembership.objects.filter(
            workspace_id=board.workspace_id,
            user_id=OuterRef("user_id"),
        )
        return (
            BoardMembership.objects.filter(board=board)
            .select_related("user")
            .annotate(is_workspace_member=Exists(workspace_membership))
        )


@extend_schema_view(
    patch=extend_schema(
        request=BoardMemberRoleSerializer,
        responses={200: BoardMembershipSerializer},
    ),
    delete=extend_schema(
        request=None,
        responses={204: OpenApiResponse(description="Membership removed.")},
    ),
)
class BoardMemberDetailView(APIView):
    def get_membership(self, workspace_id: int, board_id: int, membership_id: int):
        board = get_object_or_404(
            administered_boards_for(self.request.user),
            pk=board_id,
            workspace_id=workspace_id,
        )
        return get_object_or_404(
            BoardMembership.objects.select_related("board", "user"),
            pk=membership_id,
            board=board,
        )

    def patch(self, request, workspace_id: int, board_id: int, membership_id: int):
        membership = self.get_membership(workspace_id, board_id, membership_id)
        serializer = BoardMemberRoleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        membership = update_board_member_role(
            membership=membership,
            role=serializer.validated_data["role"],
            acting_user=request.user,
        )
        return Response(BoardMembershipSerializer(membership).data)

    def delete(self, request, workspace_id: int, board_id: int, membership_id: int):
        membership = self.get_membership(workspace_id, board_id, membership_id)
        remove_board_member(membership=membership, acting_user=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(
    request=None,
    responses={204: OpenApiResponse(description="You are no longer a member.")},
)
class BoardLeaveView(APIView):
    """Give up your own board membership; see the workspace equivalent."""

    def delete(self, request, workspace_id: int, board_id: int):
        board = get_object_or_404(
            visible_boards_for(request.user),
            pk=board_id,
            workspace_id=workspace_id,
        )
        leave_board(board=board, user=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class BoardInvitationListCreateView(generics.ListCreateAPIView):
    throttle_scope = "invitation"

    def get_serializer_class(self):
        return (
            CreateBoardInvitationSerializer
            if self.request.method == "POST"
            else BoardInvitationSerializer
        )

    def get_board(self):
        if not hasattr(self, "_board"):
            self._board = get_object_or_404(
                administered_boards_for(self.request.user),
                pk=self.kwargs["board_id"],
                workspace_id=self.kwargs["workspace_id"],
            )
        return self._board

    def get_queryset(self):
        return (
            BoardInvitation.objects.filter(board=self.get_board())
            .exclude(status=BoardInvitation.Status.ACCEPTED)
            .select_related("board__workspace", "invited_by")
        )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invitation = create_board_invitation(
            board=self.get_board(),
            email=serializer.validated_data["email"],
            role=serializer.validated_data["role"],
            invited_by=request.user,
        )
        return Response(
            BoardInvitationSerializer(invitation).data,
            status=status.HTTP_201_CREATED,
        )


@extend_schema(request=None, responses={200: BoardInvitationSerializer})
class BoardInvitationResendView(APIView):
    throttle_scope = "invitation"

    def post(self, request, workspace_id: int, board_id: int, invitation_id: int):
        board = get_object_or_404(
            administered_boards_for(request.user),
            pk=board_id,
            workspace_id=workspace_id,
        )
        invitation = get_object_or_404(
            BoardInvitation.objects.select_related("board__workspace", "invited_by"),
            pk=invitation_id,
            board=board,
        )
        invitation = resend_board_invitation(invitation=invitation, invited_by=request.user)
        return Response(BoardInvitationSerializer(invitation).data)


class CurrentUserBoardInvitationListView(generics.ListAPIView):
    serializer_class = BoardInvitationSerializer

    def get_queryset(self):
        return (
            BoardInvitation.objects.filter(
                Q(invited_user=self.request.user) | Q(email=self.request.user.email)
            )
            .exclude(status=BoardInvitation.Status.ACCEPTED)
            .select_related("board__workspace", "invited_by")
            .distinct()
        )


@extend_schema(
    request=None,
    responses={204: OpenApiResponse(description="Invitation answered.")},
)
class CurrentUserBoardInvitationResponseView(APIView):
    accept = False

    def post(self, request, invitation_id: int):
        invitation = get_object_or_404(
            BoardInvitation.objects.filter(
                Q(invited_user=request.user) | Q(email=request.user.email)
            ),
            pk=invitation_id,
        )
        respond_to_board_invitation(
            invitation=invitation,
            user=request.user,
            accept=self.accept,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class CurrentUserBoardInvitationAcceptView(CurrentUserBoardInvitationResponseView):
    accept = True


class CurrentUserBoardInvitationRejectView(CurrentUserBoardInvitationResponseView):
    accept = False


class PublicBoardInvitationView(generics.RetrieveAPIView):
    """Unauthenticated lookup of an invitation by its emailed token.

    Restricted to still-actionable invitations, so a spent token stops
    disclosing the invitee's email address.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    serializer_class = PublicBoardInvitationSerializer
    lookup_field = "token"
    lookup_url_kwarg = "token"

    def get_queryset(self):
        return BoardInvitation.objects.filter(
            status=BoardInvitation.Status.PENDING,
            expires_at__gt=timezone.now(),
        ).select_related("board__workspace")
