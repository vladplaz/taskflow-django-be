from django.urls import path

from .views import (
    CurrentUserInvitationAcceptView,
    CurrentUserInvitationListView,
    CurrentUserInvitationRejectView,
    PublicWorkspaceInvitationView,
    WorkspaceDetailView,
    WorkspaceInvitationListCreateView,
    WorkspaceInvitationResendView,
    WorkspaceLeaveView,
    WorkspaceListCreateView,
    WorkspaceMemberDetailView,
    WorkspaceMemberListView,
)


urlpatterns = [
    path("", WorkspaceListCreateView.as_view(), name="workspace-list-create"),
    path(
        "invitations/",
        CurrentUserInvitationListView.as_view(),
        name="current-user-invitation-list",
    ),
    path(
        "invitations/<int:invitation_id>/accept/",
        CurrentUserInvitationAcceptView.as_view(),
        name="current-user-invitation-accept",
    ),
    path(
        "invitations/<int:invitation_id>/reject/",
        CurrentUserInvitationRejectView.as_view(),
        name="current-user-invitation-reject",
    ),
    path(
        "invitation-links/<uuid:token>/",
        PublicWorkspaceInvitationView.as_view(),
        name="public-workspace-invitation",
    ),
    path("<int:workspace_id>/", WorkspaceDetailView.as_view(), name="workspace-detail"),
    path(
        "<int:workspace_id>/members/",
        WorkspaceMemberListView.as_view(),
        name="workspace-member-list",
    ),
    path(
        "<int:workspace_id>/members/me/",
        WorkspaceLeaveView.as_view(),
        name="workspace-leave",
    ),
    path(
        "<int:workspace_id>/members/<int:membership_id>/",
        WorkspaceMemberDetailView.as_view(),
        name="workspace-member-detail",
    ),
    path(
        "<int:workspace_id>/invitations/",
        WorkspaceInvitationListCreateView.as_view(),
        name="workspace-invitation-list-create",
    ),
    path(
        "<int:workspace_id>/invitations/<int:invitation_id>/resend/",
        WorkspaceInvitationResendView.as_view(),
        name="workspace-invitation-resend",
    ),
]
