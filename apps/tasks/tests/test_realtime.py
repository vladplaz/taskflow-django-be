from unittest.mock import Mock, patch

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from django.core.cache import cache
from django.test import TransactionTestCase, override_settings

from apps.accounts.models import User
from apps.boards.models import Board, BoardMembership
from apps.tasks.consumers import board_task_group_name, user_socket_count_key
from apps.tasks.models import BoardList, Task
from apps.tasks.realtime import publish_task_event
from apps.workspaces.models import Workspace, WorkspaceMembership
from taskflow.asgi import application


class WorkspaceTaskConsumerTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="member@example.com", password="safe-password-123"
        )
        self.workspace = Workspace.objects.create(name="Product", created_by=self.user)
        WorkspaceMembership.objects.create(
            workspace=self.workspace,
            user=self.user,
            role=WorkspaceMembership.Role.OWNER,
        )
        self.board = Board.objects.create(
            name="Website",
            workspace=self.workspace,
            created_by=self.user,
        )
        BoardMembership.objects.create(
            board=self.board,
            user=self.user,
            role=BoardMembership.Role.MEMBER,
        )

    def authenticated_headers(self, user=None):
        user = user or self.user
        session = SessionStore()
        session[SESSION_KEY] = str(user.pk)
        session[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
        session[HASH_SESSION_KEY] = user.get_session_auth_hash()
        session.save()
        return [
            (b"cookie", f"sessionid={session.session_key}".encode()),
            (b"origin", b"http://localhost:5173"),
        ]

    def test_workspace_member_receives_a_task_event(self):
        headers = self.authenticated_headers()
        event = {
            "type": "task.event",
            "workspace_id": self.workspace.id,
            "task_id": 42,
        }

        async def assert_event_is_relayed():
            communicator = WebsocketCommunicator(
                application,
                f"/ws/workspaces/{self.workspace.id}/boards/{self.board.id}/tasks/",
                headers=headers,
            )
            connected, _ = await communicator.connect()
            self.assertTrue(connected)
            await get_channel_layer().group_send(
                board_task_group_name(self.board.id),
                {"type": "task.event", "payload": event},
            )
            self.assertEqual(await communicator.receive_json_from(), event)
            await communicator.disconnect()

        async_to_sync(assert_event_is_relayed)()

    def test_anonymous_visitor_cannot_connect(self):
        communicator = WebsocketCommunicator(
            application,
            f"/ws/workspaces/{self.workspace.id}/boards/{self.board.id}/tasks/",
            headers=[(b"origin", b"http://localhost:5173")],
        )
        connected, close_code = async_to_sync(communicator.connect)()
        self.assertFalse(connected)
        self.assertEqual(close_code, 4401)

    def test_workspace_member_without_private_board_access_cannot_connect(self):
        workspace_member = User.objects.create_user(
            email="other-member@example.com",
            password="safe-password-123",
        )
        WorkspaceMembership.objects.create(
            workspace=self.workspace,
            user=workspace_member,
            role=WorkspaceMembership.Role.MEMBER,
        )
        communicator = WebsocketCommunicator(
            application,
            f"/ws/workspaces/{self.workspace.id}/boards/{self.board.id}/tasks/",
            headers=self.authenticated_headers(workspace_member),
        )

        connected, close_code = async_to_sync(communicator.connect)()

        self.assertFalse(connected)
        self.assertEqual(close_code, 4403)

    def test_publish_failure_does_not_escape_after_a_database_write(self):
        board_list = BoardList.objects.create(name="To do", board=self.board)
        task = Task.objects.create(title="Ship", board_list=board_list, created_by=self.user)
        send = Mock(side_effect=ConnectionError)

        with self.assertLogs("apps.tasks.realtime", level="ERROR"):
            with (
                patch("apps.tasks.realtime.get_channel_layer", return_value=Mock()),
                patch("apps.tasks.realtime.async_to_sync", return_value=send),
            ):
                publish_task_event(task=task, event_type="task.created", actor_id=self.user.id)

        send.assert_called_once()

    def open_socket(self, headers):
        return WebsocketCommunicator(
            application,
            f"/ws/workspaces/{self.workspace.id}/boards/{self.board.id}/tasks/",
            headers=headers,
        )

    @override_settings(WEBSOCKET_MAX_SOCKETS_PER_USER=2)
    def test_a_user_cannot_hold_more_sockets_than_the_cap_allows(self):
        headers = self.authenticated_headers()
        cache.delete(user_socket_count_key(self.user.id))
        self.addCleanup(cache.delete, user_socket_count_key(self.user.id))

        async def assert_the_third_socket_is_refused():
            accepted = [self.open_socket(headers) for _ in range(2)]
            for communicator in accepted:
                connected, _ = await communicator.connect()
                self.assertTrue(connected)

            refused = self.open_socket(headers)
            connected, close_code = await refused.connect()
            self.assertFalse(connected)
            self.assertEqual(close_code, 4429)

            # A slot given back by a disconnect goes to the next connection.
            await accepted[0].disconnect()
            reopened = self.open_socket(headers)
            connected, _ = await reopened.connect()
            self.assertTrue(connected)

            await reopened.disconnect()
            await accepted[1].disconnect()

        async_to_sync(assert_the_third_socket_is_refused)()

    @override_settings(WEBSOCKET_MAX_SOCKETS_PER_USER=0)
    def test_the_cap_is_off_when_it_is_set_to_zero(self):
        headers = self.authenticated_headers()

        async def assert_every_socket_connects():
            communicators = [self.open_socket(headers) for _ in range(4)]
            for communicator in communicators:
                connected, _ = await communicator.connect()
                self.assertTrue(connected)
            for communicator in communicators:
                await communicator.disconnect()

        async_to_sync(assert_every_socket_connects)()
