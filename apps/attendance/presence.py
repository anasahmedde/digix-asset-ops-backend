"""Where a person is clocked in, for the gates that field work sits behind.

A technician starts an installation step, photographs a visit or hands an
asset over from the site itself, so the record of their day (the attendance
register) has to say they are there. The office acts from a desk and is not
asked. The rule is a setting so a test suite, or a site with no signal, can
switch it off.
"""
from django.conf import settings
from rest_framework.exceptions import PermissionDenied

from .models import AttendanceRecord


def clocked_in_at(user, site_id) -> bool:
    """True when the person's latest attendance record is a check-in at this site."""
    last = (
        AttendanceRecord.objects.filter(user=user)
        .order_by("-created_at")
        .values_list("check_type", "site_id")
        .first()
    )
    if not last:
        return False
    check_type, where = last
    return check_type == AttendanceRecord.CheckType.CHECK_IN and (site_id is None or where == site_id)


def require_on_site(user, site):
    """Refuse field work from somebody not clocked in at the job's site."""
    if not getattr(settings, "FIELD_WORK_NEEDS_CHECK_IN", True):
        return
    if site is None:
        return
    if not clocked_in_at(user, site.pk):
        raise PermissionDenied(f"Check in at {site.name} first - the job's buttons open once you are on site.")
