from django.conf import settings
from django.db import models

from common.models import TimeStampedModel


class AttendanceRecord(TimeStampedModel):
    """A field-staff GPS check-in or check-out event."""

    class CheckType(models.TextChoices):
        CHECK_IN = "check_in", "Check In"
        CHECK_OUT = "check_out", "Check Out"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="attendance_records"
    )
    check_type = models.CharField(max_length=10, choices=CheckType.choices, default=CheckType.CHECK_IN)
    latitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    longitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    accuracy = models.FloatField(null=True, blank=True, help_text="GPS accuracy in metres")
    site = models.ForeignKey(
        "sites.Site", on_delete=models.SET_NULL, null=True, blank=True, related_name="attendance_records"
    )
    note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self):
        return f"{self.user} {self.get_check_type_display()} @ {self.created_at:%Y-%m-%d %H:%M}"
