from __future__ import annotations

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import DeviceInstallation, InstallationStep

logger = logging.getLogger(__name__)

# The default installation pipeline, in order. Every new installation gets this
# checklist so its progress can be tracked from survey through handover.
DEFAULT_STEP_TYPES = [
    InstallationStep.StepType.SURVEY,
    InstallationStep.StepType.WIRING,
    InstallationStep.StepType.STRUCTURE,
    InstallationStep.StepType.PROGRAMMING,
    InstallationStep.StepType.TESTING,
    InstallationStep.StepType.HANDOVER,
]


def seed_steps(installation: DeviceInstallation) -> int:
    """Create the default step checklist for an installation if it has none."""
    if installation.steps.exists():
        return 0
    InstallationStep.objects.bulk_create(
        [
            InstallationStep(
                installation=installation,
                step_type=step_type,
                step_number=index + 1,
            )
            for index, step_type in enumerate(DEFAULT_STEP_TYPES)
        ]
    )
    return len(DEFAULT_STEP_TYPES)


@receiver(post_save, sender=DeviceInstallation)
def create_default_installation_steps(sender, instance: DeviceInstallation, created: bool, **kwargs):
    if not created:
        return
    try:
        seed_steps(instance)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Failed to seed installation steps for %s", instance.pk)


@receiver(post_save, sender=InstallationStep)
def stamp_installation_completion(sender, instance: InstallationStep, **kwargs):
    """Keep DeviceInstallation.completed_at in sync with its step checklist.

    Stamped when every step is completed or skipped (with at least one
    completed); cleared again if a step is reopened afterwards.
    """
    from django.utils import timezone

    installation = instance.installation
    statuses = list(installation.steps.values_list("status", flat=True))
    done = (
        bool(statuses)
        and all(s in (InstallationStep.StepStatus.COMPLETED, InstallationStep.StepStatus.SKIPPED) for s in statuses)
        and any(s == InstallationStep.StepStatus.COMPLETED for s in statuses)
    )
    if done and installation.completed_at is None:
        installation.completed_at = timezone.now()
        installation.save(update_fields=["completed_at", "updated_at"])
        _anchor_client_warranties(installation)
    elif not done and installation.completed_at is not None:
        installation.completed_at = None
        installation.save(update_fields=["completed_at", "updated_at"])


def _anchor_client_warranties(installation: DeviceInstallation) -> None:
    """Client warranties run from handover: re-anchor active term-based ones."""
    from dateutil.relativedelta import relativedelta

    handover = installation.completed_at.date()
    warranties = installation.device.warranties.filter(
        warranty_type="client", status="active", months__isnull=False
    )
    for warranty in warranties:
        warranty.start_date = handover
        warranty.end_date = handover + relativedelta(months=warranty.months)
        warranty.save(update_fields=["start_date", "end_date", "updated_at"])
