from django.contrib import admin

from .models import MaintenanceRecord, MaintenanceSchedule


@admin.register(MaintenanceSchedule)
class MaintenanceScheduleAdmin(admin.ModelAdmin):
    list_display = ("title", "maintenance_type", "frequency", "next_due", "is_active", "assigned_to")
    list_filter = ("maintenance_type", "frequency", "is_active")
    search_fields = ("title",)
    raw_id_fields = ("device", "site", "assigned_to")


@admin.register(MaintenanceRecord)
class MaintenanceRecordAdmin(admin.ModelAdmin):
    list_display = ("schedule", "performed_by", "performed_at", "status", "cost")
    list_filter = ("status",)
    raw_id_fields = ("schedule", "performed_by")
