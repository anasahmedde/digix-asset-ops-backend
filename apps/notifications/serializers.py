from __future__ import annotations

from rest_framework import serializers

from .models import Notification, PushToken, WebhookEndpoint


class PushTokenSerializer(serializers.ModelSerializer):
    class Meta:
        model = PushToken
        fields = ["id", "token", "platform", "created_at"]
        read_only_fields = ["id", "created_at"]


class NotificationSerializer(serializers.ModelSerializer):
    is_resolved = serializers.BooleanField(read_only=True)

    class Meta:
        model = Notification
        fields = [
            "id",
            "recipient",
            "notification_type",
            "title",
            "message",
            "alert",
            "ticket",
            "installation",
            "data",
            "is_read",
            "read_at",
            "is_actionable",
            "is_resolved",
            "resolved_at",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class WebhookEndpointSerializer(serializers.ModelSerializer):
    secret = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = WebhookEndpoint
        fields = [
            "id",
            "name",
            "url",
            "secret",
            "events",
            "is_active",
            "created_by",
            "last_triggered",
            "failure_count",
            "created_at",
        ]
        read_only_fields = ["id", "created_at", "last_triggered", "failure_count"]
