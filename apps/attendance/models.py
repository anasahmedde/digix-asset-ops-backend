from django.conf import settings
from django.db import models

from common import clock
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
    # Kept on the phone without signal and sent later: created_at is the
    # time it happened (the phone's), uploaded_at when the server got it.
    recorded_offline = models.BooleanField(default=False)
    uploaded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self):
        return f"{self.user} {self.get_check_type_display()} @ {self.created_at:%Y-%m-%d %H:%M}"


class DutyRecord(TimeStampedModel):
    """Time in / Time out: the start and end of a field person's working day.

    Stamped with when, and where the phone was. The person sees only whether
    they are timed in; the time and place are for their supervisor (the
    person they report to) and the office, on the web.
    """

    class Kind(models.TextChoices):
        TIME_IN = "time_in", "Time in"
        TIME_OUT = "time_out", "Time out"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="duty_records"
    )
    kind = models.CharField(max_length=10, choices=Kind.choices)
    # When it happened: the phone's time when it was sent from the outbox.
    at = models.DateTimeField(default=clock.now, db_index=True)
    latitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    longitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    accuracy = models.FloatField(null=True, blank=True, help_text="GPS accuracy in metres")
    recorded_offline = models.BooleanField(default=False)
    uploaded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-at"]
        indexes = [models.Index(fields=["user", "-at"])]

    def __str__(self):
        return f"{self.user} {self.get_kind_display()} @ {self.at:%Y-%m-%d %H:%M}"
