from django.conf import settings
from django.db import models

from common.models import TimeStampedModel


class ChatRoom(TimeStampedModel):
    class RoomType(models.TextChoices):
        DIRECT = "direct", "Direct"
        GROUP = "group", "Group"

    name = models.CharField(max_length=200, blank=True)
    room_type = models.CharField(max_length=10, choices=RoomType.choices, default=RoomType.DIRECT)
    participants = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="ChatRoomMembership",
        related_name="chat_rooms",
    )
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name or f"Chat {self.id}"

    def get_other_participant(self, user):
        if self.room_type != self.RoomType.DIRECT:
            return None
        return self.participants.exclude(id=user.id).first()


class ChatRoomMembership(TimeStampedModel):
    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="chat_memberships"
    )
    last_read_at = models.DateTimeField(null=True, blank=True)
    is_muted = models.BooleanField(default=False)

    class Meta:
        unique_together = [("room", "user")]

    def __str__(self):
        return f"{self.user} in {self.room}"


class ChatMessage(TimeStampedModel):
    class MessageType(models.TextChoices):
        TEXT = "text", "Text"
        FILE = "file", "File"
        IMAGE = "image", "Image"
        SYSTEM = "system", "System"

    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sent_messages"
    )
    content = models.TextField()
    message_type = models.CharField(
        max_length=10, choices=MessageType.choices, default=MessageType.TEXT
    )
    file_url = models.URLField(max_length=500, blank=True)
    is_edited = models.BooleanField(default=False)
    edited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.sender}: {self.content[:50]}"
