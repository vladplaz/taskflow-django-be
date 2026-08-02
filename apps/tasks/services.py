from decimal import Decimal

from django.db import transaction
from django.db.models import Max
from rest_framework.exceptions import ValidationError

from apps.boards.models import BoardLabel, BoardMembership, BoardStatus

from .models import POSITION_STEP, BoardList, Task, TaskActivity, TaskAssignment, TaskComment


# Closer than this cannot be halved again at the column's precision, so the
# list is renumbered instead.
MIN_POSITION_GAP = Decimal("0.0000000001")

EDITOR_BOARD_ROLES = [BoardMembership.Role.ADMIN, BoardMembership.Role.MEMBER]


def next_position(queryset) -> Decimal:
    current_last_position = queryset.aggregate(last=Max("position"))["last"]
    return (current_last_position or Decimal("0")) + POSITION_STEP


def rebalance_positions(*, model, ordered_items: list) -> None:
    """Spread positions back out to whole steps."""
    for index, item in enumerate(ordered_items, start=1):
        item.position = POSITION_STEP * index
    model.objects.bulk_update(ordered_items, ["position"])


def position_before(*, model, ordered_items: list, before_item, error_field: str) -> Decimal:
    """Return the position that places an item directly before `before_item`.

    `ordered_items` must already exclude the item being moved and must be
    locked by the caller, so the neighbours cannot shift underneath us.
    """
    if before_item is None:
        last_position = ordered_items[-1].position if ordered_items else Decimal("0")
        return last_position + POSITION_STEP

    index = next(
        (position for position, item in enumerate(ordered_items) if item.pk == before_item.pk),
        None,
    )
    if index is None:
        # Archived or deleted between the view reading it and this
        # transaction taking its locks.
        raise ValidationError({error_field: "The item to insert before is no longer in this list."})

    previous_position = ordered_items[index - 1].position if index else Decimal("0")
    next_position_value = ordered_items[index].position
    if next_position_value - previous_position <= MIN_POSITION_GAP:
        rebalance_positions(model=model, ordered_items=ordered_items)
        previous_position = ordered_items[index - 1].position if index else Decimal("0")
        next_position_value = ordered_items[index].position
    return (previous_position + next_position_value) / 2


@transaction.atomic
def create_task(
    *,
    board_list: BoardList,
    creator,
    title: str,
    description_content: dict,
    priority: str | None,
    due_date,
    assignee_membership_ids: list[int],
    label_ids: list[int],
    status_id: int | None,
) -> Task:
    board_list = (
        BoardList.objects.select_for_update()
        .select_related("board__workspace")
        .get(pk=board_list.pk)
    )
    memberships = resolve_assignee_memberships(
        board_id=board_list.board_id,
        membership_ids=assignee_membership_ids,
    )
    status = get_task_status(board_list=board_list, status_id=status_id)
    labels = get_task_labels(board_list=board_list, label_ids=label_ids)

    task = Task.objects.create(
        board_list=board_list,
        created_by=creator,
        title=title,
        description_content=description_content,
        priority=priority,
        due_date=due_date,
        status=status,
        position=next_position(board_list.tasks.filter(is_archived=False)),
    )
    TaskAssignment.objects.bulk_create(
        [TaskAssignment(task=task, membership=membership) for membership in memberships]
    )
    task.labels.set(labels)
    record_task_activity(task=task, author=creator, action=TaskActivity.Action.CREATED)
    return task


@transaction.atomic
def archive_task(*, task: Task) -> Task:
    """Soft-delete a task so its activity and comments survive."""
    task.is_archived = True
    task.save(update_fields=["is_archived", "updated_at"])
    return task


@transaction.atomic
def restore_task(*, task: Task) -> Task:
    """Bring an archived task back, appended rather than dropped into a gap
    that no longer means what it did when the task was archived."""
    task.is_archived = False
    task.position = next_position(
        Task.objects.filter(board_list_id=task.board_list_id, is_archived=False)
    )
    task.save(update_fields=["is_archived", "position", "updated_at"])
    return task


def record_task_activity(
    *,
    task: Task,
    author,
    action: str,
    payload: dict | None = None,
) -> TaskActivity:
    return TaskActivity.objects.create(
        task=task,
        author=author,
        action=action,
        payload=payload or {},
    )


def resolve_assignee_memberships(*, board_id: int, membership_ids: list[int]):
    unique_ids = set(membership_ids)
    memberships = list(
        BoardMembership.objects.filter(
            id__in=unique_ids,
            board_id=board_id,
            role__in=EDITOR_BOARD_ROLES,
        )
    )
    if len(memberships) != len(unique_ids):
        raise ValidationError(
            {"assignee_membership_ids": "Every assignee must be an editable board member."}
        )
    return memberships


def get_task_status(*, board_list: BoardList, status_id: int | None) -> BoardStatus | None:
    if status_id is None:
        return None
    try:
        return BoardStatus.objects.get(pk=status_id, board_id=board_list.board_id)
    except BoardStatus.DoesNotExist as error:
        raise ValidationError({"status_id": "The status must belong to this board."}) from error


def replace_task_status(*, task: Task, status_id: int | None) -> None:
    task.status = get_task_status(board_list=task.board_list, status_id=status_id)
    task.save(update_fields=["status", "updated_at"])


def get_task_labels(*, board_list: BoardList, label_ids: list[int]) -> list[BoardLabel]:
    unique_label_ids = set(label_ids)
    labels = list(BoardLabel.objects.filter(id__in=unique_label_ids, board_id=board_list.board_id))
    if len(labels) != len(unique_label_ids):
        raise ValidationError({"label_ids": "Every label must belong to this board."})
    return labels


def replace_task_labels(*, task: Task, label_ids: list[int]) -> None:
    task.labels.set(get_task_labels(board_list=task.board_list, label_ids=label_ids))


@transaction.atomic
def replace_task_assignments(*, task: Task, assignee_membership_ids: list[int]) -> None:
    memberships = resolve_assignee_memberships(
        board_id=task.board_list.board_id,
        membership_ids=assignee_membership_ids,
    )
    task.assignments.all().delete()
    TaskAssignment.objects.bulk_create(
        [TaskAssignment(task=task, membership=membership) for membership in memberships]
    )
    # Drop the stale prefetch so a later read returns the new rows.
    getattr(task, "_prefetched_objects_cache", {}).pop("assignments", None)


def get_task_comment_mentions(*, task: Task, mention_user_ids: list[int]) -> list[int]:
    user_ids = set(mention_user_ids)
    mentions = list(
        BoardMembership.objects.filter(
            board_id=task.board_list.board_id,
            user_id__in=user_ids,
        ).values_list("user_id", flat=True)
    )
    if len(mentions) != len(user_ids):
        raise ValidationError(
            {"mention_user_ids": "Every mentioned user must belong to this board."}
        )
    return mentions


@transaction.atomic
def create_task_comment(
    *,
    task: Task,
    author,
    body: str,
    mention_user_ids: list[int],
) -> TaskComment:
    activity = record_task_activity(
        task=task,
        author=author,
        action=TaskActivity.Action.COMMENT_ADDED,
    )
    comment = TaskComment.objects.create(task=task, activity=activity, body=body)
    comment.mentions.set(get_task_comment_mentions(task=task, mention_user_ids=mention_user_ids))
    return comment


@transaction.atomic
def delete_task_comment(*, comment: TaskComment) -> None:
    """Remove a comment and the timeline entry it hangs off, in one
    transaction: an orphaned `comment_added` renders as an empty row."""
    activity = comment.activity
    comment.delete()
    activity.delete()


@transaction.atomic
def move_task(*, task: Task, target_board_list: BoardList, before_task: Task | None) -> Task:
    task = Task.objects.select_for_update().select_related("board_list").get(pk=task.pk)
    target_board_list = (
        BoardList.objects.select_for_update().select_related("board").get(pk=target_board_list.pk)
    )
    if target_board_list.board_id != task.board_list.board_id:
        raise ValidationError({"target_list_id": "A task can only move within its board."})
    if before_task and before_task.board_list_id != target_board_list.id:
        raise ValidationError(
            {"before_task_id": "The task to insert before must be in the target list."}
        )
    if before_task and before_task.pk == task.pk:
        raise ValidationError({"before_task_id": "A task cannot be moved before itself."})

    ordered_tasks = list(
        Task.objects.select_for_update()
        .filter(board_list=target_board_list, is_archived=False)
        .exclude(pk=task.pk)
        .order_by("position", "created_at")
    )
    task.board_list = target_board_list
    task.position = position_before(
        model=Task,
        ordered_items=ordered_tasks,
        before_item=before_task,
        error_field="before_task_id",
    )
    task.save(update_fields=["board_list", "position", "updated_at"])
    return task


@transaction.atomic
def move_board_list(
    *,
    board_list: BoardList,
    before_board_list: BoardList | None,
) -> BoardList:
    board_list = BoardList.objects.select_for_update().select_related("board").get(pk=board_list.pk)
    if before_board_list and before_board_list.board_id != board_list.board_id:
        raise ValidationError({"before_list_id": "The target list must be in the same board."})
    if before_board_list and before_board_list.pk == board_list.pk:
        raise ValidationError({"before_list_id": "A list cannot be moved before itself."})

    ordered_lists = list(
        BoardList.objects.select_for_update()
        .filter(board_id=board_list.board_id, is_archived=False)
        .exclude(pk=board_list.pk)
        .order_by("position", "created_at")
    )
    board_list.position = position_before(
        model=BoardList,
        ordered_items=ordered_lists,
        before_item=before_board_list,
        error_field="before_list_id",
    )
    board_list.save(update_fields=["position"])
    return board_list


@transaction.atomic
def archive_board_list(*, board_list: BoardList) -> BoardList:
    """Soft-delete a list.

    The tasks keep their own `is_archived` flag untouched, so restoring the
    list brings back exactly what was on it. They are hidden meanwhile because
    every task queryset filters on the list's flag too.
    """
    board_list.is_archived = True
    board_list.save(update_fields=["is_archived"])
    return board_list


@transaction.atomic
def restore_board_list(*, board_list: BoardList) -> BoardList:
    """Bring an archived list back, at the end of the board."""
    board_list.is_archived = False
    board_list.position = next_position(
        BoardList.objects.filter(board_id=board_list.board_id, is_archived=False)
    )
    board_list.save(update_fields=["is_archived", "position"])
    return board_list
