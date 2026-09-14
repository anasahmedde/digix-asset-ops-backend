from django.contrib import admin

from .models import AttendanceRecord


@admin.register(AttendanceRecord)
class AttendanceRecordAdmin(admin.ModelAdmin):
    list_display = ["user", "check_type", "site", "created_at"]
    list_filter = ["check_type", "created_at"]
    search_fields = ["user__username", "user__first_name", "user__last_name"]
    raw_id_fields = ["user", "site"]
