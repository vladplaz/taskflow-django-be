from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache

from apps.boards.access import visible_boards_for


def board_task_group_name(board_id: int) -> str:
    return f"board_{board_id}_tasks"


def user_socket_count_key(user_id: int) -> str:
    return f"ws_sockets_user_{user_id}"


class WorkspaceTaskConsumer(AsyncJsonWebsocketConsumer):
    """Streams task events for one board.

    Authorization reuses `visible_boards_for`, so a subscriber can never see a
    board the REST API would hide.
    """

    async def connect(self):
        user = self.scope["user"]
        if not user.is_authenticated:
            await self.close(code=4401)
            return

        self.workspace_id = int(self.scope["url_route"]["kwargs"]["workspace_id"])
        self.board_id = int(self.scope["url_route"]["kwargs"]["board_id"])
        if not await self.user_can_view_board(user.id, self.workspace_id, self.board_id):
            await self.close(code=4403)
            return

        if not await self.claim_socket(user.id):
            await self.close(code=4429)
            return

        self.group_name = board_task_group_name(self.board_id)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        if getattr(self, "socket_claimed", False):
            self.socket_claimed = False
            await self.release_socket()

    async def claim_socket(self, user_id: int) -> bool:
        """Take one of this user's connection slots, or report there are none.

        Counted rather than registered, so the count drifts up if a worker dies
        without disconnecting. The key's own expiry is what bounds that.
        """
        limit = settings.WEBSOCKET_MAX_SOCKETS_PER_USER
        if limit <= 0:
            return True

        self.socket_key = user_socket_count_key(user_id)
        await cache.aadd(self.socket_key, 0, settings.WEBSOCKET_SOCKET_COUNT_TTL_SECONDS)
        try:
            live = await cache.aincr(self.socket_key)
        except ValueError:
            # Expired between the two calls; this connection is the only one.
            await cache.aset(self.socket_key, 1, settings.WEBSOCKET_SOCKET_COUNT_TTL_SECONDS)
            live = 1

        if live > limit:
            await self.release_socket()
            return False

        self.socket_claimed = True
        return True

    async def release_socket(self) -> None:
        try:
            await cache.adecr(self.socket_key)
        except ValueError:
            pass

    async def task_event(self, event):
        await self.send_json(event["payload"])

    @database_sync_to_async
    def user_can_view_board(self, user_id: int, workspace_id: int, board_id: int) -> bool:
        user = get_user_model().objects.get(pk=user_id)
        return visible_boards_for(user).filter(pk=board_id, workspace_id=workspace_id).exists()
