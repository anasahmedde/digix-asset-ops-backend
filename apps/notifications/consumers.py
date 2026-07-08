from __future__ import annotations

import logging
from urllib.parse import parse_qs

from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.utils import timezone
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.tokens import AccessToken

from apps.notifications.models import Notification

logger = logging.getLogger(__name__)


class NotificationConsumer(AsyncJsonWebsocketConsumer):

    async def connect(self):
        self.user_id = None
        self.group_name = None

        query_params = parse_qs(self.scope["query_string"].decode())
        token = query_params.get("token", [None])[0]

        if not token:
            await self.close(code=4001)
            return

        try:
            access_token = AccessToken(token)
            self.user_id = str(access_token["user_id"])
        except (InvalidToken, TokenError, KeyError):
            await self.close(code=4001)
            return

        self.group_name = f"notifications_{self.user_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if self.group_name:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        msg_type = content.get("type")

        if msg_type == "mark_read":
            notification_id = content.get("notification_id")
            if notification_id and self.user_id:
                from channels.db import database_sync_to_async

                await database_sync_to_async(self._mark_read)(notification_id)

    def _mark_read(self, notification_id: str):
        Notification.objects.filter(
            id=notification_id,
            recipient_id=self.user_id,
            is_read=False,
        ).update(is_read=True, read_at=timezone.now())

    async def send_notification(self, event):
        await self.send_json(event["notification"])
