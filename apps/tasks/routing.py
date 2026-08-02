from django.urls import path

from .consumers import WorkspaceTaskConsumer


websocket_urlpatterns = [
    path(
        "ws/workspaces/<int:workspace_id>/boards/<int:board_id>/tasks/",
        WorkspaceTaskConsumer.as_asgi(),
    ),
]
