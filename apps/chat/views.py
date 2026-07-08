from django.db.models import Count, Q

from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import ChatMessage, ChatRoom, ChatRoomMembership
from .serializers import ChatMessageSerializer, ChatRoomCreateSerializer, ChatRoomSerializer


class ChatRoomViewSet(viewsets.ModelViewSet):
    serializer_class = ChatRoomSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["room_type", "is_active"]
    search_fields = ["name"]

    def get_queryset(self):
        return (
            ChatRoom.objects.filter(participants=self.request.user, is_active=True)
            .prefetch_related("participants", "memberships", "messages")
            .distinct()
        )

    def get_serializer_class(self):
        if self.action == "create":
            return ChatRoomCreateSerializer
        return ChatRoomSerializer

    def perform_create(self, serializer):
        serializer.save()

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        room = serializer.save()
        output = ChatRoomSerializer(room, context={"request": request})
        return Response(output.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def messages(self, request, pk=None):
        room = self.get_object()
        messages = room.messages.select_related("sender").all()
        page = self.paginate_queryset(messages)
        if page is not None:
            serializer = ChatMessageSerializer(page, many=True, context={"request": request})
            return self.get_paginated_response(serializer.data)
        serializer = ChatMessageSerializer(messages, many=True, context={"request": request})
        return Response(serializer.data)

    @action(detail=False, methods=["get"])
    def total_unread(self, request):
        """Total unread messages across all rooms for the current user."""
        memberships = ChatRoomMembership.objects.filter(
            user=request.user, room__is_active=True,
        ).select_related("room")

        total = 0
        for m in memberships:
            if m.last_read_at:
                total += ChatMessage.objects.filter(
                    room=m.room, created_at__gt=m.last_read_at,
                ).exclude(sender=request.user).count()
            else:
                total += ChatMessage.objects.filter(
                    room=m.room,
                ).exclude(sender=request.user).count()

        return Response({"total_unread": total})


class ChatMessageViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = ChatMessageSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return ChatMessage.objects.filter(
            room__participants=self.request.user
        ).select_related("sender").distinct()

    def perform_create(self, serializer):
        room_id = self.request.data.get("room")
        room = ChatRoom.objects.filter(
            id=room_id, participants=self.request.user
        ).first()
        if not room:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("You are not a participant of this room.")
        serializer.save(sender=self.request.user, room=room)
