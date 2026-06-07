from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import serializers

from .models import ChatMessage, ChatRoom, ChatRoomMembership

User = get_user_model()


class ChatMessageSerializer(serializers.ModelSerializer):
    sender_name = serializers.CharField(source="sender.get_full_name", read_only=True)
    sender_avatar = serializers.SerializerMethodField()

    class Meta:
        model = ChatMessage
        fields = [
            "id", "room", "sender", "sender_name", "sender_avatar",
            "content", "message_type", "file_url",
            "is_edited", "edited_at", "created_at",
        ]
        read_only_fields = ["id", "sender", "is_edited", "edited_at", "created_at"]

    def get_sender_avatar(self, obj):
        if obj.sender.avatar:
            request = self.context.get("request")
            if request:
                return request.build_absolute_uri(obj.sender.avatar.url)
            return obj.sender.avatar.url
        return None


class ChatRoomSerializer(serializers.ModelSerializer):
    last_message = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()
    participant_names = serializers.SerializerMethodField()

    class Meta:
        model = ChatRoom
        fields = [
            "id", "name", "room_type", "participants", "participant_names",
            "last_message", "unread_count", "is_active", "created_at",
        ]

    def get_last_message(self, obj):
        message = obj.messages.order_by("-created_at").first()
        if message:
            return ChatMessageSerializer(message, context=self.context).data
        return None

    def get_unread_count(self, obj):
        request = self.context.get("request")
        if not request:
            return 0
        membership = obj.memberships.filter(user=request.user).first()
        if not membership or not membership.last_read_at:
            return obj.messages.count()
        return obj.messages.filter(created_at__gt=membership.last_read_at).count()

    def get_participant_names(self, obj):
        return list(obj.participants.values_list("first_name", flat=True))


class ChatRoomCreateSerializer(serializers.Serializer):
    participant_ids = serializers.ListField(child=serializers.UUIDField())
    name = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_participant_ids(self, value):
        if not value:
            raise serializers.ValidationError("At least one participant is required.")
        users = User.objects.filter(id__in=value)
        if users.count() != len(value):
            raise serializers.ValidationError("One or more participant IDs are invalid.")
        return value

    def validate(self, attrs):
        request = self.context["request"]
        participant_ids = attrs["participant_ids"]

        if len(participant_ids) == 1:
            other_id = participant_ids[0]
            existing = (
                ChatRoom.objects.filter(room_type=ChatRoom.RoomType.DIRECT)
                .filter(participants=request.user)
                .filter(participants__id=other_id)
                .first()
            )
            if existing:
                raise serializers.ValidationError(
                    {"participant_ids": f"Direct chat already exists (room {existing.id})."}
                )

        return attrs

    def create(self, validated_data):
        request = self.context["request"]
        participant_ids = validated_data["participant_ids"]
        name = validated_data.get("name", "")

        if len(participant_ids) == 1:
            room_type = ChatRoom.RoomType.DIRECT
        else:
            room_type = ChatRoom.RoomType.GROUP

        room = ChatRoom.objects.create(name=name, room_type=room_type)

        ChatRoomMembership.objects.create(room=room, user=request.user, last_read_at=timezone.now())
        for uid in participant_ids:
            if str(uid) != str(request.user.id):
                ChatRoomMembership.objects.create(room=room, user_id=uid)
            else:
                if not room.memberships.filter(user_id=uid).exists():
                    ChatRoomMembership.objects.create(room=room, user_id=uid)

        return room
