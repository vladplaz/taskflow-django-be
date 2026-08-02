"""Attachment lifecycle: create, claim, reference, delete."""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.tasks.models import Task, TaskActivity, TaskComment
from apps.tasks.services import create_task_comment, record_task_activity

from .models import Attachment
from .storage import build_thumbnail, resolve_content_type


#: Node types a description document uses to point at an attachment.
REFERENCE_NODE_TYPES = frozenset({"attachmentImage", "attachmentLink"})


@transaction.atomic
def create_attachment(*, task: Task, uploader, upload, placement: str) -> Attachment:
    """Store one uploaded file against a task.

    Task attachments are claimed immediately; comment attachments cannot be,
    because the comment they belong to does not exist yet.
    """
    content_type = resolve_content_type(declared=upload.content_type, filename=upload.name)
    attachment = Attachment(
        task=task,
        placement=placement,
        original_name=upload.name[:255],
        content_type=content_type,
        size=upload.size,
        uploaded_by=uploader,
        claimed_at=timezone.now() if placement == Attachment.Placement.TASK else None,
    )
    attachment.file = upload

    thumbnail = build_thumbnail(upload=upload, content_type=content_type)
    if thumbnail is not None:
        attachment.thumbnail = thumbnail

    attachment.save()

    if placement == Attachment.Placement.TASK:
        record_task_activity(
            task=task,
            author=uploader,
            action=TaskActivity.Action.ATTACHMENT_ADDED,
            payload={"name": attachment.original_name},
        )
    return attachment


@transaction.atomic
def delete_attachment(*, attachment: Attachment, actor) -> None:
    """Remove an attachment; the post_delete signal removes its bytes."""
    task = attachment.task
    name = attachment.original_name
    placement = attachment.placement
    attachment.delete()
    if placement == Attachment.Placement.TASK:
        record_task_activity(
            task=task,
            author=actor,
            action=TaskActivity.Action.ATTACHMENT_REMOVED,
            payload={"name": name},
        )


def extract_attachment_ids(content) -> list[int]:
    """Attachment ids referenced by a rich-text document, in document order.

    Explicit stack, not recursion: the document is attacker-supplied JSON and
    deep nesting is cheap to send but would exhaust the interpreter's stack.
    """
    found: list[int] = []
    seen: set[int] = set()
    stack = [content]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        if node.get("type") in REFERENCE_NODE_TYPES:
            attachment_id = (node.get("attrs") or {}).get("attachmentId")
            if isinstance(attachment_id, int) and attachment_id not in seen:
                seen.add(attachment_id)
                found.append(attachment_id)
        children = node.get("content")
        if isinstance(children, list):
            stack.extend(reversed(children))
    return found


def _without_references(content, removed_ids: set[int]):
    """Rebuild a document with the named references dropped."""
    if isinstance(content, list):
        return [_without_references(item, removed_ids) for item in content]
    if not isinstance(content, dict):
        return content
    node = dict(content)
    children = node.get("content")
    if isinstance(children, list):
        node["content"] = [
            _without_references(child, removed_ids)
            for child in children
            if not (
                isinstance(child, dict)
                and child.get("type") in REFERENCE_NODE_TYPES
                and (child.get("attrs") or {}).get("attachmentId") in removed_ids
            )
        ]
    return node


def sync_description_attachments(*, task: Task | None, content):
    """Validate a description's file references and return it ready to store.

    An id belonging to another task is a 400: otherwise a member could embed,
    and then read, a file from a board they cannot see. An id with no row
    behind it is dropped instead, since that is the normal outcome of deleting
    an attachment in one tab with the task still open in another.

    Removing a reference is not a delete -- the strip owns the file.

    Errors are unscoped because the only caller is DRF's
    ``validate_description``, which files them under that field itself.
    """
    referenced = extract_attachment_ids(content)
    if not referenced:
        return content

    if task is None:
        # Nothing is uploaded against a task that does not exist yet.
        raise ValidationError(
            "Attach files to the task before referencing them in its description."
        )

    owned = set(
        Attachment.objects.filter(
            id__in=referenced,
            task=task,
            placement=Attachment.Placement.TASK,
        ).values_list("id", flat=True)
    )
    existing = set(Attachment.objects.filter(id__in=referenced).values_list("id", flat=True))

    if foreign := (existing - owned):
        raise ValidationError(
            f"These attachments do not belong to this task: "
            f"{', '.join(str(value) for value in sorted(foreign))}."
        )

    if missing := (set(referenced) - existing):
        return _without_references(content, missing)
    return content


@transaction.atomic
def replace_comment_attachments(*, comment: TaskComment, attachment_ids: list[int]) -> None:
    """Point a set of uploads at a comment, and delete the ones it lost.

    Unlike a task, a comment owns its files outright: there is no strip to fall
    back to, so losing the reference means losing the file.
    """
    requested = set(attachment_ids)
    if requested:
        claimable = Attachment.objects.filter(
            id__in=requested,
            task=comment.task,
            placement=Attachment.Placement.COMMENT,
        ).filter(
            # Already attached to this comment, so re-sending ids on edit is
            # a no-op rather than an error.
            Q(comment__isnull=True) | Q(comment=comment)
        )
        found = set(claimable.values_list("id", flat=True))
        if found != requested:
            raise ValidationError(
                {"attachment_ids": "Every attachment must be an unused upload on this task."}
            )
        claimable.update(comment=comment, claimed_at=timezone.now())

    for attachment in comment.attachments.exclude(id__in=requested):
        attachment.delete()


@transaction.atomic
def create_comment_with_attachments(
    *,
    task: Task,
    author,
    body: str,
    mention_user_ids: list[int],
    attachment_ids: list[int],
) -> TaskComment:
    """Post a comment and claim the files that were uploaded for it.

    Lives here, not in `apps.tasks.services`, so the dependency between the two
    modules points one way.
    """
    comment = create_task_comment(
        task=task,
        author=author,
        body=body,
        mention_user_ids=mention_user_ids,
    )
    replace_comment_attachments(comment=comment, attachment_ids=attachment_ids)
    return comment


def unclaimed_attachments(*, older_than_hours: int | None = None):
    """Uploads that never became part of a comment, e.g. a pasted file in a
    comment box that was never posted."""
    hours = (
        settings.ATTACHMENT_UNCLAIMED_MAX_AGE_HOURS
        if older_than_hours is None
        else older_than_hours
    )
    cutoff = timezone.now() - timedelta(hours=hours)
    return Attachment.objects.filter(claimed_at__isnull=True, created_at__lt=cutoff)
