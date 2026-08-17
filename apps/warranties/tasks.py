"""Warranty lifecycle: auto-complete once the term lapses.

Runs daily from celery beat. Active warranties past their ``end_date`` flip to
``expired`` (surfaced as "Warranty Completed"), which is what makes the
"automatically converted after N months" client requirement real — the term is
set at asset registration (months) and completion needs no human touch.
"""

import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def complete_expired_warranties():
    from apps.warranties.models import Warranty

    today = timezone.now().date()
    count = Warranty.objects.filter(
        status=Warranty.Status.ACTIVE, end_date__lt=today
    ).update(status=Warranty.Status.EXPIRED, updated_at=timezone.now())
    if count:
        logger.info("Marked %d warranty(ies) completed", count)
    return count
