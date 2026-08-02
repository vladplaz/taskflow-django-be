from django.urls import path

from .views import WorkspaceUserProfileView


urlpatterns = [
    path(
        "workspaces/<int:workspace_id>/users/<int:user_id>/",
        WorkspaceUserProfileView.as_view(),
        name="workspace-user-profile",
    ),
]
