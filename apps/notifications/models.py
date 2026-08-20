from __future__ import annotations

from django.conf import settings
from django.db import models

from common.models import TimeStampedModel


class Notification(TimeStampedModel):
    class Type(models.TextChoices):
        ALERT = "alert", "Alert"
        CHAT_MESSAGE = "chat_message", "Chat Message"
        TICKET_ASSIGNED = "ticket_assigned", "Ticket Assigned"
        TICKET_UPDATE = "ticket_update", "Ticket Update"
        TICKET_REVIEW = "ticket_review", "Ticket Review Request"
        TICKET_ESCALATED = "ticket_escalated", "Ticket Escalated"
        INSTALLATION_ASSIGNED = "installation_assigned", "Installation Assigned"
        MAINTENANCE_REMINDER = "maintenance_reminder", "Maintenance Reminder"
        SYSTEM = "system", "System"

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    notification_type = models.CharField(max_length=30, choices=Type.choices, default=Type.SYSTEM)
    title = models.CharField(max_length=300)
    message = models.TextField(blank=True)
    alert = models.ForeignKey(
        "analytics.Alert",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="notifications",
    )
    ticket = models.ForeignKey(
        "tickets.Ticket",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="notifications",
    )
    data = models.JSONField(default=dict, blank=True)
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    is_actionable = models.BooleanField(
        default=False,
        help_text="Actionable notifications stay visible until their linked entity is resolved.",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "is_read"]),
            models.Index(fields=["recipient", "-created_at"]),
            models.Index(fields=["ticket", "notification_type"]),
        ]

    @property
    def is_resolved(self):
        return self.resolved_at is not None

    def __str__(self):
        return f"{self.title} → {self.recipient}"


class WebhookEndpoint(TimeStampedModel):
    name = models.CharField(max_length=200)
    url = models.URLField(max_length=500)
    secret = models.CharField(max_length=200, blank=True)
    events = models.JSONField(default=list)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="webhook_endpoints",
    )
    last_triggered = models.DateTimeField(null=True, blank=True)
    failure_count = models.IntegerField(default=0)

    def __str__(self):
        return self.name


class WebhookLog(TimeStampedModel):
    webhook = models.ForeignKey(WebhookEndpoint, on_delete=models.CASCADE, related_name="logs")
    event = models.CharField(max_length=100)
    payload = models.JSONField(default=dict)
    response_status = models.IntegerField(null=True)
    response_body = models.TextField(blank=True)
    success = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]


class PushToken(TimeStampedModel):
    """An Expo push token for one of a user's devices (for OS-level push)."""

    class Platform(models.TextChoices):
        IOS = "ios", "iOS"
        ANDROID = "android", "Android"
        WEB = "web", "Web"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="push_tokens",
    )
    token = models.CharField(max_length=255, unique=True)
    platform = models.CharField(max_length=10, choices=Platform.choices, blank=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["user"])]

    def __str__(self):
        return f"{self.user} · {self.token[:24]}…"
