"""Installation escalation engine (ES-05, Wave 4).

Mirrors the ticket escalation engine (``apps.tickets.tasks``) for the
installation tracker: ``setup.EscalationPolicy`` rows with
``scope=installation`` drive a multi-stage due-date ladder. An installation
is overdue while ``completed_at`` is unset and its ``due_date`` has passed;
the anchor is local midnight at the end of the due date (so stage 1 with
hours=0 fires the day after the due date, stage 2 with hours=24 one day
later — every stage's ``hours`` is an offset FROM THE ANCHOR).

Each stage fires once per installation, recorded in
``DeviceInstallation.escalation_state`` as ``"<trigger>:<stage>" -> ISO
timestamp``. Firing notifies (in-app, actionable) the assigned installer
plus users holding ``escalate_to_role`` / ``also_notify_role`` (super
admins always included — same recipient rules as ticket escalation).

Runs from celery beat every 10 minutes (see ``config.celery``).
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


def _fire_stage(installation, policy, now):
    """Record the stage in the ledger, then notify installer + policy roles."""
    from apps.notifications import tasks as notification_tasks
    from apps.notifications.models import Notification
    from apps.notifications.signals import _push_ws
    from apps.tickets.tasks import _recipients

    state = dict(installation.escalation_state or {})
    state[f"{policy.trigger}:{policy.stage}"] = now.isoformat()
    installation.escalation_state = state
    installation.save(update_fields=["escalation_state", "updated_at"])

    stage_note = "" if policy.stage == 1 else f" (stage {policy.stage})"
    title = (
        f"Installation escalated{stage_note}: "
        f"{installation.device.asset_code} @ {installation.site.name}"
    )
    message = (
        f"Installation of {installation.device.asset_code} at "
        f"{installation.site.name} is past its due date ({installation.due_date})."
    )
    data = {
        "installation_id": str(installation.id),
        "device_id": str(installation.device_id),
        "site_id": str(installation.site_id),
        "due_date": str(installation.due_date),
        "stage": policy.stage,
    }

    # Assigned installer + escalate-to role + also-notify role + super admins.
    recipients = {user.pk: user for user in _recipients(policy)}
    if installation.installed_by_id:
        recipients.setdefault(installation.installed_by_id, installation.installed_by)

    for user in recipients.values():
        notif = Notification.objects.create(
            recipient=user,
            notification_type=Notification.Type.INSTALLATION_ESCALATED,
            title=title,
            message=message,
            installation=installation,
            data=data,
            is_actionable=True,
        )
        _push_ws(notif)
        notification_tasks.queue_notification_email(notif)


@shared_task
def escalate_overdue_installations():
    from apps.setup.models import EscalationPolicy
    from apps.sites.models import DeviceInstallation
    from apps.tickets.tasks import _due_date_anchor

    now = timezone.now()
    today = timezone.localdate()

    stages = list(
        EscalationPolicy.objects.filter(
            is_active=True,
            scope=EscalationPolicy.Scope.INSTALLATION,
            trigger=EscalationPolicy.Trigger.DUE_DATE,
        ).order_by("stage")
    )
    if not stages:
        return 0

    count = 0
    overdue = DeviceInstallation.objects.filter(
        completed_at__isnull=True,
        removed_at__isnull=True,
        due_date__isnull=False,
        due_date__lt=today,
    ).select_related("device", "site", "installed_by")
    for installation in overdue:
        anchor = _due_date_anchor(installation.due_date)
        for policy in stages:
            key = f"{policy.trigger}:{policy.stage}"
            if key in (installation.escalation_state or {}):
                continue
            if now < anchor + timedelta(hours=policy.hours or 0):
                continue
            _fire_stage(installation, policy, now)
            count += 1

    if count:
        logger.info("Escalated %d installation stage(s)", count)
    return count
