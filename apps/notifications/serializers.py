from __future__ import annotations

from rest_framework import serializers

from .models import CalendarEvent, Notification, PushToken, StickyNote, WebhookEndpoint


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
            "data", "link", "ref",
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


class StickyNoteSerializer(serializers.ModelSerializer):
    """A note as the board shows it: what it says, and who said it."""

    author_name = serializers.SerializerMethodField()
    mentioned_names = serializers.SerializerMethodField()
    mine = serializers.SerializerMethodField()
    # Ticked off or not; writing it stamps (or clears) done_at.
    done = serializers.BooleanField(required=False)

    def to_representation(self, obj):
        data = super().to_representation(obj)
        data["done"] = obj.done_at is not None
        return data

    def update(self, instance, validated_data):
        from django.utils import timezone

        if "done" in validated_data:
            done = validated_data.pop("done")
            instance.done_at = timezone.now() if done else None
        return super().update(instance, validated_data)

    def get_author_name(self, obj):
        return obj.author.get_full_name() or obj.author.username

    def get_mentioned_names(self, obj):
        return [u.get_full_name() or u.username for u in obj.mentions.all()]

    def get_mine(self, obj):
        user = self.context["request"].user
        return obj.author_id == user.id

    class Meta:
        model = StickyNote
        fields = [
            "id", "scope", "body", "author", "author_name", "mine",
            "mentions", "mentioned_names", "done", "done_at", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "author", "done_at", "created_at", "updated_at"]

    def validate_body(self, value):
        if not value.strip():
            raise serializers.ValidationError("Write something first.")
        return value.strip()


class CalendarEventSerializer(serializers.ModelSerializer):
    owner_name = serializers.SerializerMethodField()
    project_name = serializers.CharField(source="project.name", read_only=True, default=None)
    mine = serializers.SerializerMethodField()

    class Meta:
        model = CalendarEvent
        fields = [
            "id", "title", "date", "time", "notes", "scope", "project", "project_name",
            "remind_days_before", "owner", "owner_name", "mine", "created_at",
        ]
        read_only_fields = ["id", "owner", "created_at"]

    def get_owner_name(self, obj):
        return obj.owner.get_full_name() or obj.owner.username

    def get_mine(self, obj):
        return obj.owner_id == self.context["request"].user.id

    def validate_remind_days_before(self, value):
        if value > 60:
            raise serializers.ValidationError("Reminders go out at most 60 days ahead.")
        return value
