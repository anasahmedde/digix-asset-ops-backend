"""Ticket escalation engine (multi-stage, Wave 4).

Three independent triggers, each configured by ``setup.EscalationPolicy``
rows (data-driven — the client can retune windows/recipients without code).
A trigger may carry several stages; every stage's ``hours`` is an offset
FROM THE TRIGGER ANCHOR (stage 2 hours are absolute from the anchor, not
relative to stage 1):

- ``response_sla``  — still ``open`` past ``response_due_at`` (per-priority
  window); anchor = ``response_due_at``.
- ``assignment_sla`` — parked unassigned beyond the policy window (default
  24h); anchor = ``created_at`` while unassigned & open-ish.
- ``due_date``      — active past its ``due_date``; anchor = local midnight
  at the end of the due date (so stage 1 with hours=0 fires the day after,
  exactly like the legacy single-stage engine).

Each stage fires once per ticket, recorded in ``Ticket.escalation_state`` as
``"<trigger>:<stage>" -> ISO timestamp``. When stage 1 fires the legacy
booleans (``escalated`` / ``assignment_escalated`` / ``due_date_escalated``)
are still set — mobile/web badges depend on them. Firing adds a system
comment and notifies users holding ``escalate_to_role`` plus
``also_notify_role`` (super admins always included).

Runs from celery beat (see ``config.celery``). The UI additionally computes
``is_response_overdue`` on read, so overdue tickets are visible even between
beat runs (or if beat is not running).
"""

import logging
from datetime import datetime, time, timedelta

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
    from apps.notifications import tasks as notification_tasks
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
        notification_tasks.queue_notification_email(notif)


def _due_date_anchor(due_date):
    """Local midnight at the end of the due date (start of the next day).

    With hours=0 this reproduces the legacy behaviour: a due-date breach
    fires the day after the due date.
    """
    midnight_after = datetime.combine(due_date + timedelta(days=1), time.min)
    return timezone.make_aware(midnight_after, timezone.get_current_timezone())


def _legacy_stage1_fired(ticket, trigger):
    """Tickets escalated before the escalation_state ledger existed carry
    only the legacy boolean — treat that as stage 1 already fired."""
    from apps.setup.models import EscalationPolicy

    return {
        EscalationPolicy.Trigger.RESPONSE_SLA: ticket.escalated,
        EscalationPolicy.Trigger.ASSIGNMENT_SLA: ticket.assignment_escalated,
        EscalationPolicy.Trigger.DUE_DATE: ticket.due_date_escalated,
    }.get(trigger, False)


def _fire_stage(ticket, policy, now, comment, notif_title, notif_message):
    """Record the stage in the ledger (+ legacy booleans at stage 1), then
    comment/notify via the escalation machinery."""
    from apps.setup.models import EscalationPolicy

    state = dict(ticket.escalation_state or {})
    state[f"{policy.trigger}:{policy.stage}"] = now.isoformat()
    ticket.escalation_state = state
    update_fields = ["escalation_state", "updated_at"]

    if policy.stage == 1:
        if policy.trigger == EscalationPolicy.Trigger.RESPONSE_SLA:
            ticket.escalated = True
            ticket.escalated_at = now
            update_fields += ["escalated", "escalated_at"]
        elif policy.trigger == EscalationPolicy.Trigger.ASSIGNMENT_SLA:
            ticket.assignment_escalated = True
            ticket.assignment_escalated_at = now
            update_fields += ["assignment_escalated", "assignment_escalated_at"]
        elif policy.trigger == EscalationPolicy.Trigger.DUE_DATE:
            ticket.due_date_escalated = True
            ticket.due_date_escalated_at = now
            update_fields += ["due_date_escalated", "due_date_escalated_at"]

    ticket.save(update_fields=update_fields)
    _escalate(ticket, policy, comment, notif_title, notif_message)


@shared_task
def escalate_overdue_tickets():
    from apps.setup.models import EscalationPolicy
    from apps.tickets.models import Ticket

    now = timezone.now()
    ladders = {}
    for policy in EscalationPolicy.objects.filter(
        is_active=True, scope=EscalationPolicy.Scope.TICKET
    ).order_by("trigger", "stage"):
        ladders.setdefault(policy.trigger, []).append(policy)
    count = 0

    # ── 1. Response SLA: still open past response_due_at ──────────────
    stages = ladders.get(EscalationPolicy.Trigger.RESPONSE_SLA, [])
    if stages:
        overdue = Ticket.objects.filter(
            status=Ticket.Status.OPEN,
            response_due_at__isnull=False,
            response_due_at__lt=now,
        ).select_related("device")
        for ticket in overdue:
            for policy in stages:
                key = f"{policy.trigger}:{policy.stage}"
                if key in (ticket.escalation_state or {}):
                    continue
                if policy.stage == 1 and _legacy_stage1_fired(ticket, policy.trigger):
                    continue
                if now < ticket.response_due_at + timedelta(hours=policy.hours or 0):
                    continue
                stage_note = "" if policy.stage == 1 else f" (stage {policy.stage})"
                _fire_stage(
                    ticket, policy, now,
                    comment=(
                        f"Auto-escalated{stage_note}: no response within the "
                        f"{ticket.RESPONSE_SLA_HOURS.get(ticket.priority, 24)}h SLA for "
                        f"{ticket.get_priority_display()} priority."
                    ),
                    notif_title=f"Ticket escalated{stage_note}: {ticket.ticket_number}",
                    notif_message=f"{ticket.title} — no response within SLA.",
                )
                count += 1

    # ── 2. Assignment SLA: parked unassigned beyond the window ────────
    stages = ladders.get(EscalationPolicy.Trigger.ASSIGNMENT_SLA, [])
    if stages:
        unassigned = Ticket.objects.filter(
            status__in=ACTIVE_STATUSES,
            assigned_to__isnull=True,
            assigned_vendor__isnull=True,
            created_at__lt=now - timedelta(hours=min(p.hours or 24 for p in stages)),
        ).select_related("device")
        for ticket in unassigned:
            for policy in stages:
                key = f"{policy.trigger}:{policy.stage}"
                if key in (ticket.escalation_state or {}):
                    continue
                if policy.stage == 1 and _legacy_stage1_fired(ticket, policy.trigger):
                    continue
                window_hours = policy.hours or 24
                if now < ticket.created_at + timedelta(hours=window_hours):
                    continue
                stage_note = "" if policy.stage == 1 else f" (stage {policy.stage})"
                _fire_stage(
                    ticket, policy, now,
                    comment=f"Auto-escalated{stage_note}: still unassigned after {window_hours}h.",
                    notif_title=f"Unassigned ticket escalated{stage_note}: {ticket.ticket_number}",
                    notif_message=f"{ticket.title} — no assignee after {window_hours}h. Please assign.",
                )
                count += 1

    # ── 3. Due date: active past due_date ─────────────────────────────
    stages = ladders.get(EscalationPolicy.Trigger.DUE_DATE, [])
    if stages:
        breached = Ticket.objects.filter(
            status__in=ACTIVE_STATUSES,
            due_date__isnull=False,
            due_date__lt=now.date(),
        ).select_related("device")
        for ticket in breached:
            anchor = _due_date_anchor(ticket.due_date)
            for policy in stages:
                key = f"{policy.trigger}:{policy.stage}"
                if key in (ticket.escalation_state or {}):
                    continue
                if policy.stage == 1 and _legacy_stage1_fired(ticket, policy.trigger):
                    continue
                if now < anchor + timedelta(hours=policy.hours or 0):
                    continue
                stage_note = "" if policy.stage == 1 else f" (stage {policy.stage})"
                _fire_stage(
                    ticket, policy, now,
                    comment=f"Auto-escalated{stage_note}: past its due date ({ticket.due_date}).",
                    notif_title=f"Overdue ticket escalated{stage_note}: {ticket.ticket_number}",
                    notif_message=f"{ticket.title} — past due date {ticket.due_date}.",
                )
                count += 1

    if count:
        logger.info("Escalated %d ticket stage(s)", count)
    return count
