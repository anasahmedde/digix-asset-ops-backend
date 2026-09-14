"""Employee lifecycle: auto-deactivate accounts past their leaving date.

Runs daily from celery beat. Active users whose ``leaving_date`` has passed
lose access (``is_active=False``), so departed employees can never log in —
HR sets the date and the cutoff needs no human touch. Each deactivation is
recorded in the audit log.
"""

import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def deactivate_left_employees():
    from apps.accounts.models import AuditLog, User

    today = timezone.now().date()
    users = list(User.objects.filter(leaving_date__lt=today, is_active=True))
    for user in users:
        user.is_active = False
        user.save(update_fields=["is_active"])
        AuditLog.objects.create(
            user=None,
            action=AuditLog.Action.UPDATE,
            resource_type="user",
            resource_id=str(user.pk),
            detail={
                "auto": True,
                "reason": "Auto-deactivated: leaving date passed",
                "leaving_date": user.leaving_date.isoformat(),
            },
        )
    count = len(users)
    if count:
        logger.info("Auto-deactivated %d left employee(s)", count)
    return count
