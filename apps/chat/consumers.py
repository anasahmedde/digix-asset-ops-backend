import logging
from uuid import UUID

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.tokens import AccessToken

from .models import ChatMessage, ChatRoom, ChatRoomMembership

logger = logging.getLogger(__name__)
User = get_user_model()


class ChatConsumer(AsyncJsonWebsocketConsumer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.room_id = None
        self.room_group_name = None
        self.user = None

    async def connect(self):
        self.room_id = self.scope["url_route"]["kwargs"]["room_id"]
        self.room_group_name = f"chat_{self.room_id}"

        token = self._extract_token()
        if not token:
            await self.close(code=4001)
            return

        self.user = await self._authenticate(token)
        if not self.user:
            await self.close(code=4001)
            return

        is_participant = await self._is_participant()
        if not is_participant:
            await self.close(code=4003)
            return

        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if self.room_group_name:
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        msg_type = content.get("type")

        if msg_type == "message":
            await self._handle_message(content)
        elif msg_type == "typing":
            await self._handle_typing()
        elif msg_type == "read":
            await self._handle_read()

    async def _handle_message(self, content):
        text = content.get("content", "").strip()
        message_type = content.get("message_type", "text")
        file_url = content.get("file_url", "")

        if not text and not file_url:
            return

        message = await self._save_message(text, message_type, file_url)
        sender_name = await database_sync_to_async(self.user.get_full_name)()
        room_name = await self._get_room_display_name()

        msg_payload = {
            "id": str(message.id),
            "room": str(self.room_id),
            "sender": str(self.user.id),
            "sender_name": sender_name,
            "content": message.content,
            "message_type": message.message_type,
            "file_url": message.file_url,
            "created_at": message.created_at.isoformat(),
        }

        await self.channel_layer.group_send(
            self.room_group_name,
            {"type": "chat.message", "message": msg_payload},
        )

        participant_ids = await self._get_other_participant_ids()
        for pid in participant_ids:
            await self.channel_layer.group_send(
                f"notifications_{pid}",
                {
                    "type": "send_notification",
                    "notification": {
                        "type": "chat_message",
                        "room_id": str(self.room_id),
                        "room_name": room_name,
                        "sender_id": str(self.user.id),
                        "sender_name": sender_name,
                        "content": message.content[:200],
                        "message_id": str(message.id),
                        "created_at": message.created_at.isoformat(),
                    },
                },
            )

    async def _handle_typing(self):
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "chat.typing",
                "user_id": str(self.user.id),
                "user_name": await database_sync_to_async(self.user.get_full_name)(),
            },
        )

    async def _handle_read(self):
        await self._update_last_read()

    async def chat_message(self, event):
        await self.send_json({"type": "message", "message": event["message"]})

    async def chat_typing(self, event):
        if event["user_id"] != str(self.user.id):
            await self.send_json({
                "type": "typing",
                "user_id": event["user_id"],
                "user_name": event["user_name"],
            })

    def _extract_token(self):
        query_string = self.scope.get("query_string", b"").decode("utf-8")
        params = dict(p.split("=", 1) for p in query_string.split("&") if "=" in p)
        return params.get("token")

    @database_sync_to_async
    def _authenticate(self, raw_token):
        try:
            validated = AccessToken(raw_token)
            return User.objects.get(id=validated["user_id"])
        except (TokenError, InvalidToken, User.DoesNotExist):
            return None

    @database_sync_to_async
    def _is_participant(self):
        try:
            UUID(str(self.room_id))
        except ValueError:
            return False
        return ChatRoomMembership.objects.filter(
            room_id=self.room_id, user=self.user
        ).exists()

    @database_sync_to_async
    def _save_message(self, text, message_type, file_url):
        return ChatMessage.objects.create(
            room_id=self.room_id,
            sender=self.user,
            content=text,
            message_type=message_type,
            file_url=file_url,
        )

    @database_sync_to_async
    def _update_last_read(self):
        ChatRoomMembership.objects.filter(
            room_id=self.room_id, user=self.user
        ).update(last_read_at=timezone.now())

    @database_sync_to_async
    def _get_other_participant_ids(self):
        return list(
            ChatRoomMembership.objects.filter(room_id=self.room_id)
            .exclude(user=self.user)
            .values_list("user_id", flat=True)
        )

    @database_sync_to_async
    def _get_room_display_name(self):
        try:
            room = ChatRoom.objects.get(id=self.room_id)
            if room.name:
                return room.name
            if room.room_type == ChatRoom.RoomType.DIRECT:
                return self.user.get_full_name() or self.user.username
            return "Group Chat"
        except ChatRoom.DoesNotExist:
            return "Chat"
