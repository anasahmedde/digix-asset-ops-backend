from __future__ import annotations

from django.db.models import Q
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.tickets.models import Ticket
from common.permissions import IsSuperAdmin

from .models import Notification, PushToken, WebhookEndpoint
from .serializers import (
    NotificationSerializer,
    PushTokenSerializer,
    WebhookEndpointSerializer,
)
from .tasks import send_webhook_delivery


class PushTokenViewSet(
    mixins.CreateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """Register / de-register a device's Expo push token for the current user."""

    serializer_class = PushTokenSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "token"

    def get_queryset(self):
        return PushToken.objects.filter(user=self.request.user)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = serializer.validated_data["token"]
        obj, _ = PushToken.objects.update_or_create(
            token=token,
            defaults={
                "user": request.user,
                "platform": serializer.validated_data.get("platform", ""),
            },
        )
        return Response(PushTokenSerializer(obj).data, status=status.HTTP_201_CREATED)


class NotificationViewSet(viewsets.ModelViewSet):
    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["notification_type", "is_read", "is_actionable"]
    search_fields = ["title", "message"]
    ordering_fields = ["created_at"]

    def get_queryset(self):
        qs = Notification.objects.filter(recipient=self.request.user)
        qs = qs.exclude(is_actionable=True, resolved_at__isnull=False)
        return qs

    @action(detail=False, methods=["get"])
    def unread_count(self, request):
        count = self.get_queryset().filter(is_read=False).count()
        return Response({"count": count})

    @action(detail=True, methods=["post"])
    def mark_read(self, request, pk=None):
        notification = self.get_object()
        notification.is_read = True
        notification.read_at = timezone.now()
        notification.save(update_fields=["is_read", "read_at", "updated_at"])
        return Response(NotificationSerializer(notification).data)

    @action(detail=False, methods=["post"])
    def mark_all_read(self, request):
        updated = (
            self.get_queryset()
            .filter(is_read=False)
            .update(is_read=True, read_at=timezone.now())
        )
        return Response({"updated": updated})

    @action(detail=False, methods=["get"])
    def assigned_tickets_summary(self, request):
        """Active tickets assigned to the current user — shown as login banner."""
        tickets = Ticket.objects.filter(
            assigned_to=request.user,
        ).exclude(
            status__in=("approved", "closed"),
        ).values(
            "id", "title", "priority", "status", "category", "due_date", "created_at",
        ).order_by("-created_at")

        overdue = 0
        today = timezone.now().date()
        for t in tickets:
            if t["due_date"] and t["due_date"] < today:
                overdue += 1

        return Response({
            "total": len(tickets),
            "overdue": overdue,
            "tickets": list(tickets),
        })


class WebhookEndpointViewSet(viewsets.ModelViewSet):
    queryset = WebhookEndpoint.objects.all()
    serializer_class = WebhookEndpointSerializer
    permission_classes = [IsAuthenticated, IsSuperAdmin]
    filterset_fields = ["is_active"]
    search_fields = ["name", "url"]

    @action(detail=True, methods=["post"])
    def test(self, request, pk=None):
        webhook = self.get_object()
        payload = {
            "event": "webhook.test",
            "title": "Test webhook delivery",
            "message": "If you receive this, your webhook endpoint is configured correctly.",
        }
        send_webhook_delivery.delay(str(webhook.id), "webhook.test", payload)
        return Response({"status": "Test webhook queued for delivery."})
