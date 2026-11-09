from celery import shared_task


@shared_task
def close_forgotten_days():
    """Close check-ins and time-ins left open from an earlier day.

    Each is closed at the last work the person was seen doing that day and
    says so, so the next morning starts with a fresh check-in instead of a
    clock that has been running all night.
    """
    from .models import AttendanceRecord, DutyRecord
    from .sessions import _day_bounds, forgotten_close

    from django.utils import timezone

    today = _day_bounds(timezone.now())[0]
    closed = 0
    # ponytail: one query per person; a few hundred field staff at most.
    for user_id in AttendanceRecord.objects.values_list("user_id", flat=True).order_by().distinct():
        last = AttendanceRecord.objects.filter(user_id=user_id).order_by("-created_at").first()
        if last.check_type == AttendanceRecord.CheckType.CHECK_IN and last.created_at < today:
            out = AttendanceRecord.objects.create(
                user_id=user_id, check_type=AttendanceRecord.CheckType.CHECK_OUT, site_id=last.site_id,
                note="Closed automatically: no check-out that day. Ended at the last work recorded.",
            )
            AttendanceRecord.objects.filter(pk=out.pk).update(created_at=forgotten_close(user_id, last.created_at))
            closed += 1
    for user_id in DutyRecord.objects.values_list("user_id", flat=True).order_by().distinct():
        last = DutyRecord.objects.filter(user_id=user_id).order_by("-at").first()
        if last.kind == DutyRecord.Kind.TIME_IN and last.at < today:
            DutyRecord.objects.create(
                user_id=user_id, kind=DutyRecord.Kind.TIME_OUT, automatic=True,
                at=forgotten_close(user_id, last.at),
            )
            closed += 1
    return closed
