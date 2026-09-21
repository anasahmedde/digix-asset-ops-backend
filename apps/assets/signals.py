from __future__ import annotations

import logging

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import Device, DeviceLifecycleEvent, ProductionStep

logger = logging.getLogger(__name__)


# ── Status change journal (lifecycle event + audit trail) ────────────

@receiver(pre_save, sender=Device)
def capture_device_previous_status(sender, instance: Device, **kwargs):
    """Snapshot the stored status so post_save can tell whether it flipped."""
    if instance.pk:
        try:
            instance._previous_status = Device.objects.only("status").get(pk=instance.pk).status
        except Device.DoesNotExist:
            instance._previous_status = None
    else:
        instance._previous_status = None


@receiver(post_save, sender=Device)
def log_device_status_change(sender, instance: Device, created: bool, **kwargs):
    """Every status flip — including the initial status at registration —
    leaves a DeviceLifecycleEvent and an AuditLog row.

    The transition endpoint stashes ``_transition_user`` / ``_transition_reason``
    on the instance before saving; automatic flips (e.g. installation signals)
    log without a user.
    """
    from apps.accounts.models import AuditLog

    performed_by = getattr(instance, "_transition_user", None)

    if created:
        # Registration can plant any initial status (the registration UX
        # relies on it), so journal it too — never a silent status.
        DeviceLifecycleEvent.objects.create(
            device=instance,
            event_type=DeviceLifecycleEvent.EventType.STATUS_CHANGE,
            from_value="",
            to_value=instance.status,
            description="Registered",
            performed_by=performed_by,
        )
        AuditLog.objects.create(
            user=performed_by,
            action=AuditLog.Action.CREATE,
            resource_type="device",
            resource_id=str(instance.pk),
            detail={"from": "", "to": instance.status, "reason": "Registered"},
        )
        return

    old_status = getattr(instance, "_previous_status", None)
    if old_status is None or old_status == instance.status:
        return

    reason = getattr(instance, "_transition_reason", "")

    DeviceLifecycleEvent.objects.create(
        device=instance,
        event_type=DeviceLifecycleEvent.EventType.STATUS_CHANGE,
        from_value=old_status,
        to_value=instance.status,
        description=reason,
        performed_by=performed_by,
    )

    AuditLog.objects.create(
        user=performed_by,
        action=AuditLog.Action.UPDATE,
        resource_type="device",
        resource_id=str(instance.pk),
        detail={"from": old_status, "to": instance.status, "reason": reason},
    )


# ── A finished build puts itself into stock ──────────────────────────


@receiver(post_save, sender=ProductionStep)
def finish_build_when_route_is_done(sender, instance: ProductionStep, **kwargs):
    """Closing the last operation on an asset whose parts are all issued
    finishes the build, so the asset becomes stock without anyone editing it.

    Hooked to the step rather than to the transition endpoint so every route
    counts — a technician closing it, a work order inspected and accepted, a
    step skipped.
    """
    from .services import finish_build_if_done

    if instance.status not in (ProductionStep.Status.COMPLETED, ProductionStep.Status.SKIPPED):
        return
    try:
        finish_build_if_done(instance.device)
    except Exception:  # pragma: no cover - finishing a step must never 500
        logger.exception("Failed to finish the build for %s", instance.device_id)
