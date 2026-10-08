"""Warranty lifecycle: auto-complete once the term lapses.

Runs daily from celery beat. Active warranties past their ``end_date`` flip to
``expired`` (shown as "Expired"), which is what makes the
"automatically converted after N months" client requirement real — the term is
set at asset registration (months) and completion needs no human touch.
"""

import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def complete_expired_warranties():
    from apps.warranties.services import expire_lapsed

    count = expire_lapsed()
    if count:
        logger.info("Marked %d warranty(ies) completed", count)
    return count
