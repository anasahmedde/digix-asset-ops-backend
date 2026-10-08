from __future__ import annotations

import logging

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.assets.models import Device

from .models import DeviceInstallation, InstallationStep, Site, SiteContact

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
    """Create the step checklist for an installation if it has none.

    An asset type with a saved checklist starts from that; anything else falls
    back to the generic survey-to-handover list, so a job is never empty.
    """
    from .models import InstallationRouteTemplate

    if installation.steps.exists():
        return 0

    asset_type_id = installation.device.asset_type_id
    template = (
        InstallationRouteTemplate.objects.filter(asset_type_id=asset_type_id).first()
        if asset_type_id else None
    )
    if template is not None:
        lines = list(template.steps.all())
        if lines:
            InstallationStep.objects.bulk_create([
                InstallationStep(
                    installation=installation,
                    step_type=line.step_type,
                    custom_label=line.custom_label,
                    assigned_team=line.assigned_team,
                    description=line.description,
                    step_number=index + 1,
                )
                for index, line in enumerate(lines)
            ])
            return len(lines)

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
    if not created or getattr(instance, "_skip_default_steps", False):
        return
    try:
        seed_steps(instance)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Failed to seed installation steps for %s", instance.pk)


@receiver(post_save, sender=DeviceInstallation)
def mark_device_on_installation_track(sender, instance: DeviceInstallation, created: bool, **kwargs):
    """Creating an installation puts a pre-install asset on the installation
    track (WF-10) — the registry status flips without a manual edit.

    It moves to `assigned`, not `installed`: raising the job is not doing the
    work. The asset becomes Installed when the checklist is finished, and
    Active when the technician says so with a photo — otherwise every asset on
    the tracker would claim to be installed the moment it got there, and the
    progress the technician records would mean nothing.
    """
    if not created:
        return
    device = instance.device
    if device.status in ("procured", "in_transit", "in_stock"):
        device._transition_reason = f"Installation opened at {instance.site.name}"
        device.status = "assigned"
        device.save(update_fields=["status", "updated_at"])


@receiver(post_save, sender=Device)
def open_installation_when_assigned(sender, instance: Device, created: bool, **kwargs):
    """An assigned asset has an installation job, wherever it got assigned.

    Hooked to the asset rather than to the transition endpoint so the edit
    form counts too — an asset assigned before it had a site joins the tracker
    as soon as one is set, instead of staying invisible.
    """
    if instance.status != "assigned" or instance.current_site_id is None:
        return
    from .services import open_installation_for

    open_installation_for(instance)


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
        _mark_device_installed(installation)
    elif not done and installation.completed_at is not None:
        installation.completed_at = None
        installation.save(update_fields=["completed_at", "updated_at"])


def installation_date_for(installation: DeviceInstallation):
    """The date an installation actually went in.

    A formal handover date wins when one exists — that is the date both sides
    agreed on. Otherwise the checklist being finished is what happened, and
    failing that the date the job was booked in for.
    """
    from django.utils import timezone

    record = getattr(installation, "handover", None)
    if record is not None and record.handover_date:
        return record.handover_date
    stamp = installation.completed_at or installation.installed_at
    # localdate, not .date(): a job finished at 9pm in Karachi is still that
    # day's work, and calling .date() on the UTC stamp would book it yesterday.
    return timezone.localdate(stamp) if stamp else None


# Everything an asset can be before it goes in. "In Production" was left out,
# which stranded every in-house build: its checklist finished and nothing
# followed.
PRE_INSTALL_STATUSES = ("procured", "in_production", "in_transit", "in_stock", "assigned")


def _mark_device_installed(installation: DeviceInstallation) -> None:
    """Finishing the checklist flips a pre-install asset to Installed and
    records when it went in — the registry stays honest without anyone
    editing it by hand."""
    device = installation.device
    fields = []
    if device.status in PRE_INSTALL_STATUSES:
        device.status = "installed"
        fields.append("status")
    # Set even when the status was already moved by hand: the date belongs to
    # the installation, so it should never have to be typed in.
    if device.installation_date is None:
        installed_on = installation_date_for(installation)
        if installed_on is not None:
            device.installation_date = installed_on
            fields.append("installation_date")
    if fields:
        device.save(update_fields=[*fields, "updated_at"])


def _anchor_client_warranties(installation: DeviceInstallation) -> None:
    """Client warranties run from the day the asset goes live, not from the
    installation: this dates them once it has, and waits otherwise."""
    from apps.teams.warranties import cover_from_activation

    cover_from_activation(installation.device)


@receiver(post_save, sender=SiteContact)
@receiver(post_delete, sender=SiteContact)
def keep_the_site_s_headline_contact_in_step(sender, instance: SiteContact, **kwargs):
    """One primary contact, and the site's own fields follow it.

    A site used to be asked for a contact twice: three fields on the site
    itself, and a list of people with their designations. The form asks
    once now, for the list — but reports and exports still read the three
    fields, so they are kept true from whoever is marked primary (or, with
    nobody marked, the first one on the list).

    Both halves live in one receiver because the order matters: unmark the
    others first, then read who the primary is. Split across two, the
    headline was computed while the outgoing primary was still marked.
    """
    site = instance.site
    if getattr(instance, "is_primary", False) and instance.pk:
        SiteContact.objects.filter(site=site, is_primary=True).exclude(
            pk=instance.pk
        ).update(is_primary=False)

    lead = SiteContact.objects.filter(site=site).order_by("-is_primary", "name").first()
    fields = {
        "contact_person": lead.name if lead else "",
        "contact_phone": lead.phone if lead else "",
        "contact_email": lead.email if lead else "",
    }
    changed = {f: v for f, v in fields.items() if getattr(site, f) != v}
    if changed:
        for f, v in changed.items():
            setattr(site, f, v)
        Site.objects.filter(pk=site.pk).update(**changed)
