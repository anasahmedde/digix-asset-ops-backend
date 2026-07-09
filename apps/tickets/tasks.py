"""Ticket SLA escalation.

A ticket still ``open`` past its ``response_due_at`` is escalated once:
flagged on the record, a system comment is added, and every operations
manager is notified (WS + push via the notifications signal).

Runs from celery beat (see ``config.celery``). The UI additionally computes
``is_response_overdue`` on read, so overdue tickets are visible even between
beat runs (or if beat is not running).
"""

import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def escalate_overdue_tickets():
    from apps.accounts.models import User
    from apps.notifications.models import Notification
    from apps.tickets.models import Ticket, TicketComment

    now = timezone.now()
    overdue = Ticket.objects.filter(
        status=Ticket.Status.OPEN,
        escalated=False,
        response_due_at__lt=now,
    ).select_related("device")

    managers = list(
        User.objects.filter(role__in=("super_admin", "ops_manager"), is_active=True)
    )

    count = 0
    for ticket in overdue:
        ticket.escalated = True
        ticket.escalated_at = now
        ticket.save(update_fields=["escalated", "escalated_at", "updated_at"])

        TicketComment.objects.create(
            ticket=ticket,
            author=None,
            content=(
                f"Auto-escalated: no response within the "
                f"{ticket.RESPONSE_SLA_HOURS.get(ticket.priority, 24)}h SLA for "
                f"{ticket.get_priority_display()} priority."
            ),
            comment_type=TicketComment.CommentType.STATUS_CHANGE,
        )

        for manager in managers:
            Notification.objects.create(
                recipient=manager,
                notification_type="ticket_escalated",
                title=f"Ticket escalated: {ticket.ticket_number}",
                message=f"{ticket.title} — no response within SLA.",
                ticket=ticket,
                is_actionable=True,
            )
        count += 1

    if count:
        logger.info("Escalated %d overdue ticket(s)", count)
    return count
