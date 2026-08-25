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

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    title = models.CharField(max_length=300)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
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
    # External vendors involved in this maintenance (can be several).
    vendors = models.ManyToManyField(
        "suppliers.Supplier", blank=True, related_name="maintenance_schedules"
    )
    next_due = models.DateField()
    instructions = models.TextField(blank=True)
    # What this maintenance needs on-site, entered freely at scheduling time:
    # a list of {"name": str, "quantity": int} rows (not tied to the asset's
    # registered components).
    required_components = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.ACTIVE)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["next_due"]

    def __str__(self):
        return f"{self.title} ({self.frequency})"

    def advance_after_completion(self, performed_date):
        """Roll the schedule to its next cycle once a completed record lands."""
        from dateutil.relativedelta import relativedelta

        if self.frequency == self.Frequency.ONE_TIME:
            self.status = self.Status.COMPLETED
            self.is_active = False
            self.save(update_fields=["status", "is_active", "updated_at"])
            return
        deltas = {
            self.Frequency.DAILY: relativedelta(days=1),
            self.Frequency.WEEKLY: relativedelta(weeks=1),
            self.Frequency.MONTHLY: relativedelta(months=1),
            self.Frequency.QUARTERLY: relativedelta(months=3),
            self.Frequency.YEARLY: relativedelta(years=1),
        }
        base = max(self.next_due, performed_date) if self.next_due else performed_date
        self.next_due = base + deltas[self.frequency]
        self.status = self.Status.ACTIVE
        self.save(update_fields=["next_due", "status", "updated_at"])

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
    # Which of the asset's components were serviced/replaced during the visit.
    components_used = models.ManyToManyField(
        "assets.AssetComponent", blank=True, related_name="maintenance_records"
    )

    class Meta:
        ordering = ["-performed_at"]

    def __str__(self):
        return f"{self.schedule.title} - {self.performed_at.date()}"


class MaintenanceRecordPhoto(TimeStampedModel):
    record = models.ForeignKey(MaintenanceRecord, on_delete=models.CASCADE, related_name="photos")
    image = models.ImageField(upload_to="maintenance/photos/")
    caption = models.CharField(max_length=300, blank=True)
    taken_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True
    )

    def __str__(self):
        return f"Photo for {self.record}"
