"""How long somebody was actually at a site.

A check-in starts the clock and a check-out stops it. People forget the
second one, and a check-in left open would otherwise run on for days. So
time is counted day by day: a day without a check-out ends at the last
thing the person was seen doing that day (a job step, a photo, a cost, a
check-in elsewhere), and a later day without a check-in starts at the
first. Those ends are marked as reckoned, so whoever reads the hours knows
which were recorded and which were worked out.
"""
from datetime import datetime, time, timedelta

from django.utils import timezone

from .models import AttendanceRecord


def _day_bounds(t):
    day = timezone.localtime(t).date()
    start = timezone.make_aware(datetime.combine(day, time.min))
    return start, start + timedelta(days=1) - timedelta(microseconds=1)


def _seen(user_id, since, until):
    """Every moment this person was recorded doing something, in a window."""
    from apps.maintenance.models import MaintenanceActivity
    from apps.sites.models import InstallationActivity

    out = list(
        AttendanceRecord.objects.filter(user_id=user_id, created_at__gte=since, created_at__lte=until)
        .values_list("created_at", flat=True)
    )
    for model in (MaintenanceActivity, InstallationActivity):
        out += model.objects.filter(actor_id=user_id, at__gte=since, at__lte=until).values_list("at", flat=True)
    return sorted(out)


def last_seen(user_id, since, until):
    """The last moment this person was seen working, or `since` if never."""
    seen = _seen(user_id, since, until)
    return seen[-1] if seen else since


def forgotten_close(user_id, opened):
    """When something opened on a past day and never closed is taken to have ended.

    At least a second after it opened, so the close is always the latest record.
    """
    return max(last_seen(user_id, opened, _day_bounds(opened)[1]), opened + timedelta(seconds=1))


def spells(user_id, site_id, start, end):
    """Each day's time this person was checked in at this site, touching [start, end].

    A list of {"from", "to", "reckoned"}; "reckoned" is True where an end
    came from the day's work rather than a check-in or check-out.
    """
    recs = list(
        AttendanceRecord.objects.filter(
            user_id=user_id, created_at__gte=start - timedelta(days=3), created_at__lte=end + timedelta(days=1),
        ).order_by("created_at").values_list("check_type", "site_id", "created_at")
    )
    now = timezone.now()
    out = []
    for i, (kind, where, cin) in enumerate(recs):
        if kind != AttendanceRecord.CheckType.CHECK_IN or where != site_id:
            continue
        # The next record of any kind ends it: a check-out, or a check-in
        # somewhere else, which means they had left.
        cout = recs[i + 1][2] if i + 1 < len(recs) else None
        day_from, last_day = cin, cout or min(now, end)
        while day_from <= last_day:
            day_start, day_end = _day_bounds(day_from)
            if day_from != cin:
                # A later day with no check-in: from the first thing they did.
                seen = _seen(user_id, day_start, min(day_end, last_day))
                if not seen:
                    day_from = day_end + timedelta(microseconds=1)
                    continue
                day_from = seen[0]
            if cout is not None and cout <= day_end:
                to, reckoned = cout, day_from != cin
            elif cout is None and day_end >= now:
                to, reckoned = now, day_from != cin  # still there
            else:
                to, reckoned = last_seen(user_id, day_from, day_end), True
            if to >= start and day_from <= end and to > day_from:
                out.append({"from": day_from, "to": to, "reckoned": reckoned})
            day_from = day_end + timedelta(microseconds=1)
    return out


def hours(found) -> float:
    return round(sum((s["to"] - s["from"]).total_seconds() for s in found) / 3600, 2)
