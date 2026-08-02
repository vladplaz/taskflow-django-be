from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q, prefetch_related_objects
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, permissions
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.attachments.models import Attachment
from apps.attachments.services import create_comment_with_attachments
from apps.boards.access import editable_boards_for, visible_boards_for
from apps.boards.models import Board, BoardMembership
from apps.workspaces.models import WorkspaceMembership
from taskflow.pagination import OffsetPagination
from taskflow.views import ARCHIVED_PARAMETER, ArchiveFilterMixin

from .models import BoardList, Task, TaskActivity, TaskComment
from .realtime import publish_board_list_event, publish_task_event
from .serializers import (
    BoardListSerializer,
    MoveBoardListSerializer,
    MoveTaskSerializer,
    TaskActivitySerializer,
    TaskCommentSerializer,
    TaskDetailSerializer,
    TaskSerializer,
)
from .services import (
    archive_board_list,
    archive_task,
    create_task,
    delete_task_comment,
    move_board_list,
    move_task,
    next_position,
    record_task_activity,
    restore_board_list,
    restore_task,
)


#: The card shows a count, not the rows. Comment attachments belong to the
#: timeline, not the card.
STRIP_ATTACHMENT_COUNT = Count(
    "attachments",
    filter=Q(attachments__placement=Attachment.Placement.TASK),
    distinct=True,
)


class BoardAccessMixin:
    """Shared board-scoped lookups for every task endpoint."""

    def board_queryset(self, *, edit: bool):
        return (
            editable_boards_for(self.request.user)
            if edit
            else visible_boards_for(self.request.user)
        )

    def needs_edit_access(self) -> bool:
        return self.request.method not in permissions.SAFE_METHODS

    def get_serializer_context(self):
        """Add the workspace id every attachment URL is built from.

        The lookup rather than a bare `super()` call because this mixin is used
        with plain `APIView`s, which have no context to extend.
        """
        inherited = getattr(super(), "get_serializer_context", None)
        context = inherited() if inherited else {"request": self.request, "view": self}
        context["workspace_id"] = self.kwargs["workspace_id"]
        return context

    def get_board(self, board_id: int, *, edit: bool) -> Board:
        if not hasattr(self, "_board"):
            self._board = get_object_or_404(
                self.board_queryset(edit=edit),
                pk=board_id,
                workspace_id=self.kwargs["workspace_id"],
            )
        return self._board

    def task_queryset(self, *, edit: bool, archived: bool = False):
        # `board_list__is_archived` matters as much as the task's own flag,
        # or an archived list's tasks stay reachable through /tasks/<id>/.
        return Task.objects.filter(
            is_archived=archived,
            board_list__is_archived=False,
            board_list__board__is_archived=False,
            board_list__board__workspace_id=self.kwargs["workspace_id"],
            board_list__board__in=self.board_queryset(edit=edit),
        )


@extend_schema_view(get=extend_schema(parameters=[ARCHIVED_PARAMETER]))
class BoardListListCreateView(ArchiveFilterMixin, BoardAccessMixin, generics.ListCreateAPIView):
    serializer_class = BoardListSerializer

    def get_board_for_request(self) -> Board:
        return self.get_board(self.kwargs["board_id"], edit=self.needs_edit_access())

    def get_queryset(self):
        return BoardList.objects.filter(
            board=self.get_board_for_request(),
            is_archived=self.wants_archived(),
        )

    def perform_create(self, serializer):
        # Lock the board: two concurrent creates would allocate one position.
        with transaction.atomic():
            board = Board.objects.select_for_update().get(pk=self.get_board_for_request().pk)
            serializer.save(
                board=board,
                position=next_position(board.lists.filter(is_archived=False)),
            )
        publish_board_list_event(
            board_list=serializer.instance,
            event_type="list.created",
            actor_id=self.request.user.id,
        )


class BoardListDetailView(BoardAccessMixin, generics.RetrieveUpdateDestroyAPIView):
    """Rename or archive a single list."""

    serializer_class = BoardListSerializer
    lookup_url_kwarg = "list_id"

    def get_queryset(self):
        board = self.get_board(self.kwargs["board_id"], edit=self.needs_edit_access())
        return BoardList.objects.filter(board=board, is_archived=False)

    def perform_update(self, serializer):
        board_list = serializer.save()
        publish_board_list_event(
            board_list=board_list,
            event_type="list.updated",
            actor_id=self.request.user.id,
        )

    def perform_destroy(self, instance):
        archive_board_list(board_list=instance)
        publish_board_list_event(
            board_list=instance,
            event_type="list.archived",
            actor_id=self.request.user.id,
        )


@extend_schema(request=None, responses={200: BoardListSerializer})
class BoardListRestoreView(BoardAccessMixin, APIView):
    """Bring an archived list back to the end of the board."""

    def post(self, request, workspace_id: int, board_id: int, list_id: int):
        board = self.get_board(board_id, edit=True)
        board_list = get_object_or_404(
            BoardList.objects.filter(is_archived=True),
            pk=list_id,
            board=board,
        )
        restored = restore_board_list(board_list=board_list)
        publish_board_list_event(
            board_list=restored,
            event_type="list.restored",
            actor_id=request.user.id,
        )
        return Response(BoardListSerializer(restored).data)


@extend_schema(
    request=MoveBoardListSerializer,
    responses={200: BoardListSerializer},
)
class BoardListMoveView(BoardAccessMixin, APIView):
    def post(self, request, workspace_id: int, board_id: int, list_id: int):
        board = self.get_board(board_id, edit=True)
        board_list = get_object_or_404(
            BoardList.objects.filter(is_archived=False),
            pk=list_id,
            board=board,
        )
        serializer = MoveBoardListSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        before_board_list = None
        if before_list_id := serializer.validated_data.get("before_list_id"):
            before_board_list = get_object_or_404(
                BoardList.objects.filter(is_archived=False, board=board),
                pk=before_list_id,
            )
        moved_board_list = move_board_list(
            board_list=board_list,
            before_board_list=before_board_list,
        )
        publish_board_list_event(
            board_list=moved_board_list,
            event_type="list.moved",
            actor_id=request.user.id,
        )
        return Response(BoardListSerializer(moved_board_list).data)


@extend_schema_view(get=extend_schema(parameters=[ARCHIVED_PARAMETER]))
class TaskListCreateView(ArchiveFilterMixin, BoardAccessMixin, generics.ListCreateAPIView):
    serializer_class = TaskSerializer

    def get_board_list(self) -> BoardList:
        if not hasattr(self, "_board_list"):
            self._board_list = get_object_or_404(
                BoardList.objects.filter(
                    is_archived=False,
                    board__is_archived=False,
                    board__workspace_id=self.kwargs["workspace_id"],
                    board__in=self.board_queryset(edit=self.needs_edit_access()),
                ),
                pk=self.kwargs["list_id"],
            )
        return self._board_list

    def get_queryset(self):
        return (
            Task.objects.filter(
                board_list=self.get_board_list(),
                is_archived=self.wants_archived(),
            )
            .select_related("created_by", "status")
            .prefetch_related("assignments__membership__user", "labels")
            .annotate(attachment_count=STRIP_ATTACHMENT_COUNT)
            # Aggregating drops Meta.ordering (Django 4.0 stopped folding it
            # into the GROUP BY) and unordered querysets paginate randomly.
            .order_by("position", "created_at")
        )

    def perform_create(self, serializer):
        data = serializer.validated_data
        task = create_task(
            board_list=self.get_board_list(),
            creator=self.request.user,
            title=data["title"],
            description_content=data.get("description_content", {}),
            priority=data.get("priority"),
            due_date=data.get("due_date"),
            assignee_membership_ids=data.get("assignee_membership_ids", []),
            label_ids=data.get("label_ids", []),
            status_id=data.get("status_id"),
        )
        task = (
            Task.objects.select_related("status")
            .prefetch_related("assignments__membership__user", "labels")
            .annotate(attachment_count=STRIP_ATTACHMENT_COUNT)
            .get(pk=task.pk)
        )
        serializer.instance = task
        publish_task_event(
            task=task,
            event_type="task.created",
            actor_id=self.request.user.id,
        )


class TaskDetailView(BoardAccessMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = TaskDetailSerializer

    def get_queryset(self):
        return (
            self.task_queryset(edit=self.needs_edit_access())
            .select_related("status", "board_list__board")
            .prefetch_related(*TaskDetailSerializer.TASK_PREFETCHES)
        )

    def update(self, request, *args, **kwargs):
        """Serialize the response from a warm prefetch cache.

        DRF's UpdateModelMixin empties `_prefetched_objects_cache` after
        `perform_update`, but TaskDetailSerializer.update has already refreshed
        those relations, so dropping it only costs a query per assignee.
        """
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        prefetch_related_objects(
            [serializer.instance],
            *TaskDetailSerializer.TASK_PREFETCHES,
        )
        return Response(serializer.data)

    def perform_update(self, serializer):
        task = serializer.save()
        publish_task_event(
            task=task,
            event_type="task.updated",
            actor_id=self.request.user.id,
        )

    def perform_destroy(self, instance):
        archive_task(task=instance)
        publish_task_event(
            task=instance,
            event_type="task.archived",
            actor_id=self.request.user.id,
        )


@extend_schema(request=None, responses={200: TaskDetailSerializer})
class TaskRestoreView(BoardAccessMixin, APIView):
    """Bring an archived task back to the end of its list."""

    def post(self, request, workspace_id: int, task_id: int):
        task = get_object_or_404(
            self.task_queryset(edit=True, archived=True).select_related("board_list__board"),
            pk=task_id,
        )
        restored = restore_task(task=task)
        publish_task_event(
            task=restored,
            event_type="task.restored",
            actor_id=request.user.id,
        )
        restored = (
            Task.objects.select_related("status")
            .prefetch_related(*TaskDetailSerializer.TASK_PREFETCHES)
            .get(pk=restored.pk)
        )
        return Response(TaskDetailSerializer(restored, context=self.get_serializer_context()).data)


class TaskActivityListView(BoardAccessMixin, generics.ListAPIView):
    serializer_class = TaskActivitySerializer
    # Newest first, and read in uneven batches -- see OffsetPagination.
    pagination_class = OffsetPagination

    def get_queryset(self):
        task = get_object_or_404(self.task_queryset(edit=False), pk=self.kwargs["task_id"])
        # An author who has since left keeps their entries; the client
        # renders them differently, so resolve membership per row.
        current_board_membership = BoardMembership.objects.filter(
            board_id=OuterRef("task__board_list__board_id"),
            user_id=OuterRef("author_id"),
        )
        current_workspace_membership = WorkspaceMembership.objects.filter(
            workspace_id=OuterRef("task__board_list__board__workspace_id"),
            user_id=OuterRef("author_id"),
        )
        return (
            TaskActivity.objects.filter(task=task)
            .annotate(
                author_is_member=Exists(current_board_membership)
                | Exists(current_workspace_membership)
            )
            .select_related("author", "comment")
            .prefetch_related("comment__mentions", "comment__attachments__uploaded_by")
        )


class TaskCommentCreateView(BoardAccessMixin, generics.CreateAPIView):
    serializer_class = TaskCommentSerializer

    def get_task(self) -> Task:
        if not hasattr(self, "_task"):
            self._task = get_object_or_404(
                self.task_queryset(edit=True),
                pk=self.kwargs["task_id"],
            )
        return self._task

    def perform_create(self, serializer):
        comment = create_comment_with_attachments(
            task=self.get_task(),
            author=self.request.user,
            body=serializer.validated_data["body"],
            mention_user_ids=serializer.validated_data.get("mention_user_ids", []),
            attachment_ids=serializer.validated_data.get("attachment_ids", []),
        )
        serializer.instance = comment
        publish_task_event(
            task=comment.task,
            event_type="task.comment_created",
            actor_id=self.request.user.id,
        )


class TaskCommentDetailView(BoardAccessMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = TaskCommentSerializer

    def get_queryset(self):
        return (
            TaskComment.objects.filter(
                task__is_archived=False,
                task__board_list__is_archived=False,
                task__board_list__board__is_archived=False,
                task__board_list__board__workspace_id=self.kwargs["workspace_id"],
                task__board_list__board__in=self.board_queryset(edit=self.needs_edit_access()),
            )
            .select_related("activity", "task__board_list__board")
            .prefetch_related("attachments__uploaded_by")
        )

    def get_object(self):
        comment = super().get_object()
        # Authorship gates writing, not reading.
        if self.needs_edit_access() and comment.activity.author_id != self.request.user.id:
            raise PermissionDenied("Only the comment author can edit or delete it.")
        return comment

    def perform_destroy(self, instance):
        task = instance.task
        delete_task_comment(comment=instance)
        publish_task_event(
            task=task,
            event_type="task.comment_deleted",
            actor_id=self.request.user.id,
        )

    def perform_update(self, serializer):
        comment = serializer.save()
        publish_task_event(
            task=comment.task,
            event_type="task.comment_updated",
            actor_id=self.request.user.id,
        )


@extend_schema(request=MoveTaskSerializer, responses={200: TaskSerializer})
class TaskMoveView(BoardAccessMixin, APIView):
    def post(self, request, workspace_id: int, task_id: int):
        task = get_object_or_404(
            self.task_queryset(edit=True).select_related("board_list__board"),
            pk=task_id,
        )
        previous_list_name = task.board_list.name
        previous_list_id = task.board_list_id

        serializer = MoveTaskSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        target_board_list = get_object_or_404(
            BoardList.objects.filter(is_archived=False, board__is_archived=False),
            pk=serializer.validated_data["target_list_id"],
            board=task.board_list.board,
        )
        before_task = None
        if before_task_id := serializer.validated_data.get("before_task_id"):
            before_task = get_object_or_404(
                Task.objects.filter(
                    is_archived=False,
                    board_list__is_archived=False,
                    board_list__board=task.board_list.board,
                ),
                pk=before_task_id,
            )

        moved_task = move_task(
            task=task,
            target_board_list=target_board_list,
            before_task=before_task,
        )
        if moved_task.board_list_id != previous_list_id:
            record_task_activity(
                task=moved_task,
                author=request.user,
                action=TaskActivity.Action.MOVED,
                payload={"from": previous_list_name, "to": target_board_list.name},
            )
        moved_task = (
            Task.objects.select_related("status")
            .prefetch_related("assignments__membership__user", "labels")
            .get(pk=moved_task.pk)
        )
        publish_task_event(
            task=moved_task,
            event_type="task.moved",
            actor_id=request.user.id,
        )
        return Response(TaskSerializer(moved_task).data)
