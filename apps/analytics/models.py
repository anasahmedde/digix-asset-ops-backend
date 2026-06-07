from django.conf import settings
from django.db import models

from common.models import TimeStampedModel


class Alert(TimeStampedModel):
    class Severity(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        ERROR = "error", "Error"
        CRITICAL = "critical", "Critical"

    class Category(models.TextChoices):
        DEVICE_STATUS = "device_status", "Device Status"
        MAINTENANCE_DUE = "maintenance_due", "Maintenance Due"
        WARRANTY_EXPIRY = "warranty_expiry", "Warranty Expiry"
        INVENTORY_LOW = "inventory_low", "Low Inventory"
        TICKET_OVERDUE = "ticket_overdue", "Ticket Overdue"
        SYSTEM = "system", "System"

    title = models.CharField(max_length=300)
    message = models.TextField(blank=True)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.INFO)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.SYSTEM)
    device = models.ForeignKey(
        "assets.Device", on_delete=models.CASCADE, null=True, blank=True, related_name="alerts"
    )
    site = models.ForeignKey(
        "sites.Site", on_delete=models.CASCADE, null=True, blank=True, related_name="alerts"
    )
    is_read = models.BooleanField(default=False)
    is_dismissed = models.BooleanField(default=False)
    read_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="read_alerts"
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"[{self.severity}] {self.title}"


class SavedReport(TimeStampedModel):
    class ReportType(models.TextChoices):
        ASSET_SUMMARY = "asset_summary", "Asset Summary"
        MAINTENANCE = "maintenance", "Maintenance Report"
        FINANCIAL = "financial", "Financial Report"
        INSTALLATION = "installation", "Installation Report"
        CUSTOM = "custom", "Custom Report"

    name = models.CharField(max_length=300)
    report_type = models.CharField(max_length=20, choices=ReportType.choices)
    parameters = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_reports"
    )
    is_scheduled = models.BooleanField(default=False)
    schedule_cron = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return self.name
