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
