"""Authorization predicates for boards.

Every entry point composes these querysets instead of re-deriving the rules.
"""

from django.db.models import OuterRef, Q, QuerySet, Subquery

from apps.workspaces.models import WorkspaceMembership

from .models import Board, BoardMembership


WORKSPACE_ADMIN_ROLES = WorkspaceMembership.ADMIN_ROLES
BOARD_EDITOR_ROLES = BoardMembership.EDITOR_ROLES


def _scoped(predicate: Q, *, include_archived: bool) -> QuerySet[Board]:
    """Apply an access predicate, hiding archived boards unless asked.

    Only the archive listing and the restore endpoint pass ``include_archived``.
    """
    queryset = Board.objects.filter(predicate)
    if not include_archived:
        queryset = queryset.filter(is_archived=False)
    return queryset.distinct()


def visible_boards_for(user, *, include_archived: bool = False) -> QuerySet[Board]:
    """Boards the user may read."""
    if not user.is_authenticated:
        return Board.objects.none()
    return _scoped(
        Q(memberships__user=user)
        | Q(
            workspace__memberships__user=user,
            workspace__memberships__role__in=WORKSPACE_ADMIN_ROLES,
        )
        | Q(
            workspace__memberships__user=user,
            visibility=Board.Visibility.WORKSPACE,
        ),
        include_archived=include_archived,
    )


def editable_boards_for(user, *, include_archived: bool = False) -> QuerySet[Board]:
    """Boards whose content the user may change."""
    if not user.is_authenticated:
        return Board.objects.none()
    return _scoped(
        Q(memberships__user=user, memberships__role__in=BOARD_EDITOR_ROLES)
        | Q(
            workspace__memberships__user=user,
            workspace__memberships__role__in=WORKSPACE_ADMIN_ROLES,
        ),
        include_archived=include_archived,
    )


def administered_boards_for(user, *, include_archived: bool = False) -> QuerySet[Board]:
    """Boards whose settings and membership the user may change."""
    if not user.is_authenticated:
        return Board.objects.none()
    return _scoped(
        Q(memberships__user=user, memberships__role=BoardMembership.Role.ADMIN)
        | Q(
            workspace__memberships__user=user,
            workspace__memberships__role__in=WORKSPACE_ADMIN_ROLES,
        ),
        include_archived=include_archived,
    )


def with_viewer_roles(queryset: QuerySet[Board], user) -> QuerySet[Board]:
    """Attach the requesting user's workspace and board roles to each row,
    sparing the serializer three queries per board."""
    if not user.is_authenticated:
        return queryset
    return queryset.annotate(
        viewer_workspace_role=Subquery(
            WorkspaceMembership.objects.filter(
                workspace_id=OuterRef("workspace_id"),
                user=user,
            ).values("role")[:1]
        ),
        viewer_board_role=Subquery(
            BoardMembership.objects.filter(
                board_id=OuterRef("pk"),
                user=user,
            ).values("role")[:1]
        ),
    )


def access_role(*, workspace_role: str | None, board_role: str | None, visibility: str):
    """Collapse a user's two memberships into one effective board role."""
    if workspace_role in WORKSPACE_ADMIN_ROLES:
        return BoardMembership.Role.ADMIN
    if board_role:
        return board_role
    if workspace_role and visibility == Board.Visibility.WORKSPACE:
        return BoardMembership.Role.VIEWER
    return None
