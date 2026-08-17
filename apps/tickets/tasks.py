"""Ticket escalation engine.

Three independent triggers, each configured by a ``setup.EscalationPolicy``
row (data-driven — the client can retune windows/recipients without code):

- ``response_sla``  — still ``open`` past ``response_due_at`` (per-priority window)
- ``assignment_sla`` — parked unassigned beyond the policy window (default 24h)
- ``due_date``      — active past its ``due_date``

Each trigger fires once per ticket: the record is flagged, a system comment is
added, and users holding ``escalate_to_role`` (default Group Head) plus
``also_notify_role`` (default Operations) are notified over WS + push.

Runs from celery beat (see ``config.celery``). The UI additionally computes
``is_response_overdue`` on read, so overdue tickets are visible even between
beat runs (or if beat is not running).
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)

# Statuses that still need work — escalation only chases these.
ACTIVE_STATUSES = (
    "open", "in_progress", "on_hold", "blocked", "alignment_pending",
    "pending_ops_approval", "pending_client_approval", "pending_review",
)


def _recipients(policy):
    """Users to notify for a policy: escalate-to role + optional extra role."""
    from apps.accounts.models import User

    roles = {policy.escalate_to_role}
    if policy.also_notify_role:
        roles.add(policy.also_notify_role)
    # super_admin always stays in the loop
    roles.add("super_admin")
    return list(User.objects.filter(role__in=roles, is_active=True))


def _escalate(ticket, policy, comment, notif_title, notif_message):
    from apps.notifications.models import Notification
    from apps.notifications.signals import _push_ws
    from apps.tickets.models import TicketComment

    TicketComment.objects.create(
        ticket=ticket,
        author=None,
        content=comment,
        comment_type=TicketComment.CommentType.STATUS_CHANGE,
    )
    for user in _recipients(policy):
        notif = Notification.objects.create(
            recipient=user,
            notification_type="ticket_escalated",
            title=notif_title,
            message=notif_message,
            ticket=ticket,
            is_actionable=True,
        )
        _push_ws(notif)


@shared_task
def escalate_overdue_tickets():
    from apps.setup.models import EscalationPolicy
    from apps.tickets.models import Ticket

    now = timezone.now()
    policies = {p.trigger: p for p in EscalationPolicy.objects.filter(is_active=True)}
    count = 0

    # ── 1. Response SLA: still open past response_due_at ──────────────
    policy = policies.get(EscalationPolicy.Trigger.RESPONSE_SLA)
    if policy:
        overdue = Ticket.objects.filter(
            status=Ticket.Status.OPEN,
            escalated=False,
            response_due_at__lt=now,
        ).select_related("device")
        for ticket in overdue:
            ticket.escalated = True
            ticket.escalated_at = now
            ticket.save(update_fields=["escalated", "escalated_at", "updated_at"])
            _escalate(
                ticket, policy,
                comment=(
                    f"Auto-escalated: no response within the "
                    f"{ticket.RESPONSE_SLA_HOURS.get(ticket.priority, 24)}h SLA for "
                    f"{ticket.get_priority_display()} priority."
                ),
                notif_title=f"Ticket escalated: {ticket.ticket_number}",
                notif_message=f"{ticket.title} — no response within SLA.",
            )
            count += 1

    # ── 2. Assignment SLA: parked unassigned beyond the window ────────
    policy = policies.get(EscalationPolicy.Trigger.ASSIGNMENT_SLA)
    if policy:
        window = timedelta(hours=policy.hours or 24)
        unassigned = Ticket.objects.filter(
            status__in=ACTIVE_STATUSES,
            assigned_to__isnull=True,
            assigned_vendor__isnull=True,
            assignment_escalated=False,
            created_at__lt=now - window,
        ).select_related("device")
        for ticket in unassigned:
            ticket.assignment_escalated = True
            ticket.assignment_escalated_at = now
            ticket.save(update_fields=["assignment_escalated", "assignment_escalated_at", "updated_at"])
            _escalate(
                ticket, policy,
                comment=f"Auto-escalated: still unassigned after {policy.hours or 24}h.",
                notif_title=f"Unassigned ticket escalated: {ticket.ticket_number}",
                notif_message=f"{ticket.title} — no assignee after {policy.hours or 24}h. Please assign.",
            )
            count += 1

    # ── 3. Due date: active past due_date ─────────────────────────────
    policy = policies.get(EscalationPolicy.Trigger.DUE_DATE)
    if policy:
        breached = Ticket.objects.filter(
            status__in=ACTIVE_STATUSES,
            due_date__isnull=False,
            due_date__lt=now.date(),
            due_date_escalated=False,
        ).select_related("device")
        for ticket in breached:
            ticket.due_date_escalated = True
            ticket.due_date_escalated_at = now
            ticket.save(update_fields=["due_date_escalated", "due_date_escalated_at", "updated_at"])
            _escalate(
                ticket, policy,
                comment=f"Auto-escalated: past its due date ({ticket.due_date}).",
                notif_title=f"Overdue ticket escalated: {ticket.ticket_number}",
                notif_message=f"{ticket.title} — past due date {ticket.due_date}.",
            )
            count += 1

    if count:
        logger.info("Escalated %d ticket(s)", count)
    return count
