from django.contrib import admin

from .models import Ticket


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ("title", "priority", "status", "category", "assigned_to", "site", "due_date", "created_at")
    list_filter = ("status", "priority", "category")
    search_fields = ("title", "description")
    raw_id_fields = ("device", "site", "assigned_to", "reported_by")
