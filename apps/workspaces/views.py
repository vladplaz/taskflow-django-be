from django.db.models import OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Workspace, WorkspaceInvitation, WorkspaceMembership
from .serializers import (
    CreateWorkspaceInvitationSerializer,
    PublicWorkspaceInvitationSerializer,
    WorkspaceInvitationSerializer,
    WorkspaceMemberRoleSerializer,
    WorkspaceMemberSerializer,
    WorkspaceSerializer,
)
from .services import (
    WORKSPACE_ADMIN_ROLES,
    create_workspace,
    create_workspace_invitation,
    delete_workspace,
    leave_workspace,
    remove_workspace_member,
    resend_workspace_invitation,
    respond_to_workspace_invitation,
    update_workspace_member_role,
)


def membership_role_subquery(user):
    """The requesting user's role, attached to each workspace row."""
    return Subquery(
        WorkspaceMembership.objects.filter(
            workspace=OuterRef("pk"),
            user=user,
        ).values("role")[:1]
    )


class WorkspaceListCreateView(generics.ListCreateAPIView):
    serializer_class = WorkspaceSerializer

    def get_queryset(self):
        return Workspace.objects.filter(memberships__user=self.request.user).annotate(
            membership_role=membership_role_subquery(self.request.user)
        )

    def perform_create(self, serializer):
        workspace = create_workspace(
            name=serializer.validated_data["name"],
            description=serializer.validated_data.get("description", ""),
            creator=self.request.user,
        )
        workspace.membership_role = WorkspaceMembership.Role.OWNER
        serializer.instance = workspace


class WorkspaceAdminMixin:
    """Resolves a workspace the requesting user administers, once per request."""

    def get_admin_workspace(self, workspace_id: int | None = None) -> Workspace:
        if not hasattr(self, "_workspace"):
            self._workspace = get_object_or_404(
                Workspace.objects.filter(
                    memberships__user=self.request.user,
                    memberships__role__in=WORKSPACE_ADMIN_ROLES,
                ),
                pk=workspace_id if workspace_id is not None else self.kwargs["workspace_id"],
            )
        return self._workspace


class WorkspaceDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Read or rename a workspace; only its owner may delete it."""

    serializer_class = WorkspaceSerializer
    lookup_url_kwarg = "workspace_id"

    def required_roles(self) -> list[str] | None:
        """Roles the requester must hold, or None when any member may proceed."""
        if self.request.method == "DELETE":
            return [WorkspaceMembership.Role.OWNER]
        if self.request.method not in permissions.SAFE_METHODS:
            return WORKSPACE_ADMIN_ROLES
        return None

    def get_queryset(self):
        # One filter() call, not two: chaining joins `memberships` twice and
        # lets one row match the user while another matches the role.
        conditions = {"memberships__user": self.request.user}
        roles = self.required_roles()
        if roles is not None:
            conditions["memberships__role__in"] = roles
        return (
            Workspace.objects.filter(**conditions)
            .annotate(membership_role=membership_role_subquery(self.request.user))
            .distinct()
        )

    def perform_destroy(self, instance):
        delete_workspace(workspace=instance)


class WorkspaceMemberListView(WorkspaceAdminMixin, generics.ListAPIView):
    serializer_class = WorkspaceMemberSerializer

    def get_queryset(self):
        return WorkspaceMembership.objects.filter(
            workspace=self.get_admin_workspace()
        ).select_related("user")


@extend_schema_view(
    patch=extend_schema(
        request=WorkspaceMemberRoleSerializer,
        responses={200: WorkspaceMemberSerializer},
    ),
    delete=extend_schema(
        request=None,
        responses={204: OpenApiResponse(description="Membership removed.")},
    ),
)
class WorkspaceMemberDetailView(WorkspaceAdminMixin, APIView):
    def get_membership(self, workspace_id: int, membership_id: int) -> WorkspaceMembership:
        return get_object_or_404(
            WorkspaceMembership.objects.select_related("workspace", "user"),
            pk=membership_id,
            workspace=self.get_admin_workspace(workspace_id),
        )

    def patch(self, request, workspace_id: int, membership_id: int):
        membership = self.get_membership(workspace_id, membership_id)
        serializer = WorkspaceMemberRoleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        membership = update_workspace_member_role(
            membership=membership,
            role=serializer.validated_data["role"],
            acting_user=request.user,
        )
        return Response(WorkspaceMemberSerializer(membership).data)

    def delete(self, request, workspace_id: int, membership_id: int):
        membership = self.get_membership(workspace_id, membership_id)
        remove_workspace_member(membership=membership, acting_user=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(
    request=None,
    responses={204: OpenApiResponse(description="You are no longer a member.")},
)
class WorkspaceLeaveView(APIView):
    """Give up your own membership; the member-detail endpoint is admin-only
    and refuses self-removal."""

    def delete(self, request, workspace_id: int):
        workspace = get_object_or_404(
            Workspace.objects.filter(memberships__user=request.user),
            pk=workspace_id,
        )
        leave_workspace(workspace=workspace, user=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class WorkspaceInvitationListCreateView(WorkspaceAdminMixin, generics.ListCreateAPIView):
    throttle_scope = "invitation"

    def get_serializer_class(self):
        if self.request.method == "POST":
            return CreateWorkspaceInvitationSerializer
        return WorkspaceInvitationSerializer

    def get_queryset(self):
        return (
            WorkspaceInvitation.objects.filter(workspace=self.get_admin_workspace())
            .exclude(status=WorkspaceInvitation.Status.ACCEPTED)
            .select_related("workspace", "invited_by")
        )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invitation = create_workspace_invitation(
            workspace=self.get_admin_workspace(),
            email=serializer.validated_data["email"],
            invited_by=request.user,
        )
        return Response(
            WorkspaceInvitationSerializer(invitation).data,
            status=status.HTTP_201_CREATED,
        )


@extend_schema(request=None, responses={200: WorkspaceInvitationSerializer})
class WorkspaceInvitationResendView(WorkspaceAdminMixin, APIView):
    throttle_scope = "invitation"

    def post(self, request, workspace_id: int, invitation_id: int):
        invitation = get_object_or_404(
            WorkspaceInvitation.objects.select_related("workspace", "invited_by"),
            pk=invitation_id,
            workspace=self.get_admin_workspace(workspace_id),
        )
        invitation = resend_workspace_invitation(
            invitation=invitation,
            invited_by=request.user,
        )
        return Response(WorkspaceInvitationSerializer(invitation).data)


@extend_schema_view(get=extend_schema(operation_id="workspaces_my_invitations_list"))
class CurrentUserInvitationListView(generics.ListAPIView):
    serializer_class = WorkspaceInvitationSerializer

    def get_queryset(self):
        return (
            WorkspaceInvitation.objects.filter(
                Q(invited_user=self.request.user) | Q(email=self.request.user.email)
            )
            .exclude(status=WorkspaceInvitation.Status.ACCEPTED)
            .select_related("workspace", "invited_by")
            .distinct()
        )


@extend_schema(
    request=None,
    responses={204: OpenApiResponse(description="Invitation answered.")},
)
class CurrentUserInvitationResponseView(APIView):
    accept = False

    def post(self, request, invitation_id: int):
        invitation = get_object_or_404(
            WorkspaceInvitation.objects.filter(
                Q(invited_user=request.user) | Q(email=request.user.email)
            ),
            pk=invitation_id,
        )
        respond_to_workspace_invitation(
            invitation=invitation,
            user=request.user,
            accept=self.accept,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class CurrentUserInvitationAcceptView(CurrentUserInvitationResponseView):
    accept = True


class CurrentUserInvitationRejectView(CurrentUserInvitationResponseView):
    accept = False


class PublicWorkspaceInvitationView(generics.RetrieveAPIView):
    """Unauthenticated lookup of an invitation by its emailed token.

    Restricted to still-actionable invitations, so a spent token stops
    disclosing the invitee's email address.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    serializer_class = PublicWorkspaceInvitationSerializer
    lookup_field = "token"
    lookup_url_kwarg = "token"

    def get_queryset(self):
        return WorkspaceInvitation.objects.filter(
            status=WorkspaceInvitation.Status.PENDING,
            expires_at__gt=timezone.now(),
        ).select_related("workspace")
