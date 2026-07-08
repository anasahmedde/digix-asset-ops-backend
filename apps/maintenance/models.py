from django.conf import settings
from django.db import models

from common.models import TimeStampedModel


class MaintenanceSchedule(TimeStampedModel):
    class MaintenanceType(models.TextChoices):
        PREVENTIVE = "preventive", "Preventive"
        CORRECTIVE = "corrective", "Corrective"
        PREDICTIVE = "predictive", "Predictive"

    class Frequency(models.TextChoices):
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"
        MONTHLY = "monthly", "Monthly"
        QUARTERLY = "quarterly", "Quarterly"
        YEARLY = "yearly", "Yearly"
        ONE_TIME = "one_time", "One-Time"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PENDING = "pending", "Pending"
        IN_PROCESS = "in_process", "In Process"
        ON_HOLD = "on_hold", "On Hold"
        OVERDUE = "overdue", "Over Due"
        COMPLETED = "completed", "Completed"

    title = models.CharField(max_length=300)
    maintenance_type = models.CharField(max_length=15, choices=MaintenanceType.choices, default=MaintenanceType.PREVENTIVE)
    frequency = models.CharField(max_length=15, choices=Frequency.choices, default=Frequency.MONTHLY)
    device = models.ForeignKey(
        "assets.Device", on_delete=models.CASCADE, null=True, blank=True, related_name="maintenance_schedules"
    )
    site = models.ForeignKey(
        "sites.Site", on_delete=models.SET_NULL, null=True, blank=True, related_name="maintenance_schedules"
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="maintenance_assignments"
    )
    next_due = models.DateField()
    instructions = models.TextField(blank=True)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.ACTIVE)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["next_due"]

    def __str__(self):
        return f"{self.title} ({self.frequency})"

    @property
    def effective_status(self):
        """Auto-flag overdue schedules that aren't completed or on hold."""
        from django.utils import timezone

        if self.status in (self.Status.COMPLETED, self.Status.ON_HOLD):
            return self.status
        if self.next_due and self.next_due < timezone.now().date():
            return self.Status.OVERDUE
        return self.status


class MaintenanceRecord(TimeStampedModel):
    class Status(models.TextChoices):
        COMPLETED = "completed", "Completed"
        SKIPPED = "skipped", "Skipped"
        PARTIAL = "partial", "Partial"

    schedule = models.ForeignKey(
        MaintenanceSchedule, on_delete=models.CASCADE, related_name="records"
    )
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="maintenance_records"
    )
    performed_at = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.COMPLETED)
    notes = models.TextField(blank=True)
    cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    class Meta:
        ordering = ["-performed_at"]

    def __str__(self):
        return f"{self.schedule.title} - {self.performed_at.date()}"
