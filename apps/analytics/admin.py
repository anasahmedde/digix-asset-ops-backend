from django.contrib import admin

from .models import Alert, SavedReport


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ["title", "severity", "category", "is_read", "is_dismissed", "created_at"]
    list_filter = ["severity", "category", "is_read", "is_dismissed"]
    search_fields = ["title", "message"]


@admin.register(SavedReport)
class SavedReportAdmin(admin.ModelAdmin):
    list_display = ["name", "report_type", "created_by", "is_scheduled", "created_at"]
    list_filter = ["report_type", "is_scheduled"]
