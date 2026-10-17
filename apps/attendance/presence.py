"""Where a person is clocked in, for the gates that field work sits behind.

A technician starts an installation step, photographs a visit or hands an
asset over from the site itself, so the record of their day (the attendance
register) has to say they are there. The office acts from a desk and is not
asked. The rule is a setting so a test suite, or a site with no signal, can
switch it off.
"""
from django.conf import settings
from django.utils import timezone

from common import clock
from rest_framework.exceptions import PermissionDenied, ValidationError

from .models import AttendanceRecord


def on_duty(user) -> bool:
    """Timed in (Time in / Time out), as of the act: an offline check-in is
    judged by whether they had timed in by then."""
    from .models import DutyRecord

    last = (
        DutyRecord.objects.filter(user=user, at__lte=clock.now())
        .order_by("-at").values_list("kind", flat=True).first()
    )
    return last == DutyRecord.Kind.TIME_IN


def clocked_in_at(user, site_id) -> bool:
    """True when the person's latest attendance record is a check-in at this site."""
    # Judged as of the act: a scan recorded offline at 10:30 asks where the
    # person was at 10:30, not where they checked in since.
    last = (
        AttendanceRecord.objects.filter(user=user, created_at__lte=clock.now())
        .order_by("-created_at")
        .values_list("check_type", "site_id")
        .first()
    )
    if not last:
        return False
    check_type, where = last
    return check_type == AttendanceRecord.CheckType.CHECK_IN and (site_id is None or where == site_id)


def require_verified_asset(stamped, device):
    """Refuse work on a job whose asset the phone has not scanned yet.

    ``stamped`` is the visit or installation carrying ``asset_verified_at``.
    A job with no asset has nothing to scan.
    """
    if not getattr(settings, "FIELD_WORK_NEEDS_CHECK_IN", True):
        return
    if device is None or stamped.asset_verified_at:
        return
    raise PermissionDenied(
        f"Scan the QR label on {device.asset_code} first - the buttons open once "
        "the phone has confirmed you are at the right asset."
    )


def verify_asset(user, stamped, device, code):
    """The code the phone scanned is this job's asset, or it is refused."""
    code = (code or "").strip()
    if device is None:
        raise ValidationError({"detail": "This job has no asset to verify."})
    if not code:
        raise ValidationError({"code": ["Scan the asset's QR label."]})
    accepted = {device.asset_code.casefold()}
    if device.serial_number:
        accepted.add(device.serial_number.casefold())
    if code.casefold() not in accepted:
        raise ValidationError({"detail": (
            f"That label reads {code}, not {device.asset_code}. "
            "This is not the asset this job is for."
        )})
    stamped.asset_verified_at = clock.now()
    stamped.asset_verified_by = user
    stamped.save(update_fields=["asset_verified_at", "asset_verified_by", "updated_at"])
    return stamped


def require_on_site(user, site):
    """Refuse field work from somebody not clocked in at the job's site."""
    if not getattr(settings, "FIELD_WORK_NEEDS_CHECK_IN", True):
        return
    if site is None:
        return
    if not clocked_in_at(user, site.pk):
        raise PermissionDenied(f"Check in at {site.name} first - the job's buttons open once you are on site.")
