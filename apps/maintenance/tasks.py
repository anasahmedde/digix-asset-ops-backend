"""Maintenance due alerts (MW alerts, Wave 4).

Every 6 hours (celery beat, see ``config.celery``) this scans active
maintenance schedules (``is_active=True``, status not completed) whose
``next_due`` falls within the next 7 days and raises:

- an analytics ``Alert`` (category ``maintenance_due``, severity
  ``warning``) naming the schedule and its due date — deduped: skipped while
  an unread alert with the same category+device already exists (same
  category+message for site-only schedules without a device);
- an in-app ``maintenance_reminder`` Notification to the schedule's
  assignee when one is set — deduped per schedule per due cycle via the
  message containing ``next_due``.

Creating the Alert also fans out to admins through the existing
``Alert -> Notification`` signal in ``apps.notifications.signals``.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)

DUE_SOON_DAYS = 7


@shared_task
def generate_maintenance_due_alerts():
    from apps.analytics.models import Alert
    from apps.maintenance.models import MaintenanceSchedule
    from apps.notifications.models import Notification
    from apps.notifications.signals import _push_ws

    today = timezone.localdate()
    horizon = today + timedelta(days=DUE_SOON_DAYS)

    due_soon = (
        MaintenanceSchedule.objects.filter(is_active=True, next_due__lte=horizon)
        .exclude(status=MaintenanceSchedule.Status.COMPLETED)
        .select_related("device", "site", "assigned_to")
    )

    alerts_created = 0
    reminders_created = 0
    for schedule in due_soon:
        title = f"Maintenance due: {schedule.title}"
        # The "(ref …)" token keys the dedupe to this schedule AND this due
        # cycle — two schedules on one device (or one schedule rolling to its
        # next cycle) each get their own alert.
        cycle_ref = f"(ref {schedule.pk} due {schedule.next_due})"
        message = (
            f"{schedule.title}"
            + (f" — {schedule.device.asset_code}" if schedule.device_id else "")
            + (f" @ {schedule.site.name}" if schedule.site_id else "")
            + f" is due on {schedule.next_due}. {cycle_ref}"
        )

        # ── Alert (deduped per schedule per cycle while unread) ───────
        already_alerted = Alert.objects.filter(
            category=Alert.Category.MAINTENANCE_DUE,
            message__contains=cycle_ref,
            is_read=False,
        ).exists()
        if not already_alerted:
            Alert.objects.create(
                title=title,
                message=message,
                severity=Alert.Severity.WARNING,
                category=Alert.Category.MAINTENANCE_DUE,
                device=schedule.device,
                site=schedule.site,
            )
            alerts_created += 1

        # ── Assignee reminder (once per schedule per due cycle) ───────
        if schedule.assigned_to_id:
            # Keyed on the schedule id + cycle (not title text) so identically
            # titled schedules for one assignee each still remind.
            already_reminded = Notification.objects.filter(
                recipient_id=schedule.assigned_to_id,
                notification_type=Notification.Type.MAINTENANCE_REMINDER,
                data__schedule_id=str(schedule.id),
                data__next_due=str(schedule.next_due),
            ).exists()
            if not already_reminded:
                data = {
                    "schedule_id": str(schedule.id),
                    "priority": schedule.priority,
                    "next_due": str(schedule.next_due),
                }
                if schedule.device_id:
                    data["device_id"] = str(schedule.device_id)
                if schedule.site_id:
                    data["site_id"] = str(schedule.site_id)
                notif = Notification.objects.create(
                    recipient_id=schedule.assigned_to_id,
                    notification_type=Notification.Type.MAINTENANCE_REMINDER,
                    title=title,
                    message=message,
                    data=data,
                    is_actionable=True,
                )
                _push_ws(notif)
                reminders_created += 1

    if alerts_created or reminders_created:
        logger.info(
            "Maintenance due sweep: %d alert(s), %d reminder(s)",
            alerts_created, reminders_created,
        )
    return alerts_created + reminders_created
