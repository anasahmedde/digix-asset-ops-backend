from __future__ import annotations

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.assets.models import Device

from .services import close_corrective_jobs, open_corrective_job

logger = logging.getLogger(__name__)

OUT_OF_SERVICE = Device.Status.UNDER_MAINTENANCE


@receiver(post_save, sender=Device)
def keep_maintenance_register_in_step(sender, instance: Device, created: bool, **kwargs):
    """An asset going out of service raises a corrective job; coming back
    closes it.

    Hooked to the model rather than the transition endpoint so every route
    that moves an asset — the registry, the installation tracker, a scheduled
    task — keeps the two registers saying the same thing. The assets app's
    pre_save receiver has already stashed the previous status.
    """
    if created:
        return
    previous = getattr(instance, "_previous_status", None)
    if previous is None or previous == instance.status:
        return

    user = getattr(instance, "_transition_user", None)
    reason = getattr(instance, "_transition_reason", "")

    try:
        if instance.status == OUT_OF_SERVICE:
            open_corrective_job(
                instance, user, reason, getattr(instance, "_maintenance_details", None)
            )
        elif previous == OUT_OF_SERVICE:
            close_corrective_jobs(
                instance, user, reason,
                returned_to_service=instance.status == Device.Status.ACTIVE,
            )
    except Exception:  # pragma: no cover - a status change must never 500
        logger.exception("Failed to sync the maintenance register for %s", instance.pk)


@receiver(post_save, sender="maintenance.MaintenanceSchedule")
def keep_a_round_open(sender, instance, **kwargs):
    """A live schedule always has a round to plan against.

    Whoever attends is decided round by round, so the row has to exist before
    anybody can be put on it — from the moment the schedule is written down,
    and again the moment a completed round rolls it to the next cycle.
    """
    from .models import MaintenanceSchedule

    if not instance.is_active or instance.status == MaintenanceSchedule.Status.COMPLETED:
        return
    try:
        instance.open_visit()
    except Exception:  # pragma: no cover - never block saving a schedule
        logger.exception("Could not open the next round for schedule %s", instance.pk)
