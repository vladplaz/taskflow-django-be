from django.urls import path

from .views import AttachmentDetailView, TaskAttachmentUploadView


urlpatterns = [
    path(
        "workspaces/<int:workspace_id>/tasks/<int:task_id>/attachments/",
        TaskAttachmentUploadView.as_view(),
        name="task-attachment-upload",
    ),
    # Not nested under the task: the client holds an attachment id long after
    # it stops holding the task.
    path(
        "workspaces/<int:workspace_id>/attachments/<int:pk>/",
        AttachmentDetailView.as_view(),
        name="attachment-detail",
    ),
]
