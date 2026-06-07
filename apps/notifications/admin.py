from django.contrib import admin

from .models import Notification, WebhookEndpoint, WebhookLog


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ["title", "recipient", "notification_type", "is_read", "is_actionable", "resolved_at", "created_at"]
    list_filter = ["notification_type", "is_read", "is_actionable"]
    search_fields = ["title", "message"]
    raw_id_fields = ["recipient", "alert", "ticket"]


@admin.register(WebhookEndpoint)
class WebhookEndpointAdmin(admin.ModelAdmin):
    list_display = ["name", "url", "is_active", "failure_count", "last_triggered", "created_at"]
    list_filter = ["is_active"]
    search_fields = ["name", "url"]


@admin.register(WebhookLog)
class WebhookLogAdmin(admin.ModelAdmin):
    list_display = ["webhook", "event", "response_status", "success", "created_at"]
    list_filter = ["success", "event"]
    raw_id_fields = ["webhook"]
