"""Helpers shared by the app test suites."""

from django.contrib.auth import get_user_model

from apps.boards.models import Board, BoardMembership
from apps.tasks.models import BoardList, Task
from apps.workspaces.models import Workspace, WorkspaceMembership


DEFAULT_PASSWORD = "safe-password-123"


def results(response):
    """Rows out of a paginated list response."""
    return response.data["results"]


def create_user(email: str, display_name: str | None = None, **extra):
    return get_user_model().objects.create_user(
        email=email,
        password=extra.pop("password", DEFAULT_PASSWORD),
        display_name=display_name or email.split("@")[0].title(),
        **extra,
    )


def create_workspace(*, owner, name: str = "Product", **extra) -> Workspace:
    """A workspace plus its owner membership, mirroring the real service."""
    workspace = Workspace.objects.create(name=name, created_by=owner, **extra)
    WorkspaceMembership.objects.create(
        workspace=workspace,
        user=owner,
        role=WorkspaceMembership.Role.OWNER,
    )
    return workspace


def add_workspace_member(*, workspace, user, role=WorkspaceMembership.Role.MEMBER):
    return WorkspaceMembership.objects.create(workspace=workspace, user=user, role=role)


def create_board(*, workspace, creator, name: str = "Roadmap", **extra) -> Board:
    """A board plus an admin membership for its creator."""
    board = Board.objects.create(
        workspace=workspace,
        name=name,
        created_by=creator,
        **extra,
    )
    BoardMembership.objects.create(
        board=board,
        user=creator,
        role=BoardMembership.Role.ADMIN,
    )
    return board


def add_board_member(*, board, user, role=BoardMembership.Role.MEMBER):
    return BoardMembership.objects.create(board=board, user=user, role=role)


def create_board_list(*, board, name: str = "Todo", **extra) -> BoardList:
    return BoardList.objects.create(board=board, name=name, **extra)


def create_task(*, board_list, creator, title: str = "Task", **extra) -> Task:
    return Task.objects.create(
        board_list=board_list,
        title=title,
        created_by=creator,
        **extra,
    )
