"""Keeping the maintenance register in step with an asset going out of service.

Standard corrective-maintenance practice: an asset that stops working is taken
out of service and a job is raised against it, the work is done, a completion
record is filed, and the asset is returned to service. Putting an asset "under
maintenance" in the registry *is* the first of those events, so the job is
raised from it rather than being typed in twice — otherwise the registry says
the asset is being worked on and the maintenance register has never heard of it.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from .models import MaintenanceRecord, MaintenanceSchedule

logger = logging.getLogger(__name__)

OPEN_STATUSES = (
    MaintenanceSchedule.Status.ACTIVE,
    MaintenanceSchedule.Status.PENDING,
    MaintenanceSchedule.Status.IN_PROCESS,
    MaintenanceSchedule.Status.ON_HOLD,
    MaintenanceSchedule.Status.OVERDUE,
)


def open_corrective_jobs(device):
    """The unfinished corrective jobs raised against an asset."""
    return MaintenanceSchedule.objects.filter(
        device=device,
        maintenance_type=MaintenanceSchedule.MaintenanceType.CORRECTIVE,
        status__in=OPEN_STATUSES,
    )


def open_corrective_job(device, user=None, reason: str = "", details=None):
    """Raise a corrective job for an asset that has just gone out of service.

    Idempotent: an asset can be taken out of service from several places, and
    one outage is one job. Returns the job — existing or new.
    """
    existing = open_corrective_jobs(device).first()
    if existing is not None:
        return existing

    note = (reason or "").strip()
    details = details or {}
    return MaintenanceSchedule.objects.create(
        title=(note[:300] or f"Corrective maintenance — {device.asset_code}"),
        # Unplanned downtime defaults to high: the asset is not earning while
        # it sits. The person raising it can say otherwise.
        priority=details.get("priority") or MaintenanceSchedule.Priority.HIGH,
        maintenance_type=MaintenanceSchedule.MaintenanceType.CORRECTIVE,
        # A breakdown is a one-off, not a recurring cycle — completing it
        # closes the job instead of rolling it to a next due date.
        frequency=MaintenanceSchedule.Frequency.ONE_TIME,
        device=device,
        site=device.current_site,
        assigned_to=details.get("assigned_to") or device.assigned_technician,
        # The date the repair was promised by — what "overdue" is measured against.
        next_due=details.get("next_due") or timezone.localdate(),
        status=MaintenanceSchedule.Status.IN_PROCESS,
        instructions=(details.get("instructions") or "").strip() or note,
    )


def close_corrective_jobs(device, user=None, reason: str = "", *, returned_to_service=True):
    """File the completion record(s) for an asset coming back out of maintenance.

    Whether it returns to service or goes on to RMA, the maintenance visit is
    over and the register should say so. Billability follows the asset's
    warranty state, exactly as a hand-entered record does.
    """
    from apps.warranties.services import derive_billability

    jobs = list(open_corrective_jobs(device))
    if not jobs:
        return []

    _, billable, charge = derive_billability(device)
    note = (reason or "").strip() or (
        "Returned to service" if returned_to_service else "Closed — asset left maintenance"
    )
    now = timezone.now()

    records = []
    for job in jobs:
        records.append(
            MaintenanceRecord.objects.create(
                schedule=job,
                performed_by=user,
                performed_at=now,
                status=MaintenanceRecord.Status.COMPLETED,
                notes=note,
                is_billable=billable,
                charge_to=charge,
            )
        )
        # One-time jobs close out here; anything recurring rolls forward.
        job.advance_after_completion(now.date())
    return records


def return_to_service_if_done(record, user=None):
    """Put the asset back in service when its corrective job is completed.

    The technician finishes the repair in the maintenance register, not in the
    asset registry, so completing the job is what brings the asset back to
    Active. Only when nothing else is still wrong with it: an asset with a
    second open fault stays out of service.
    """
    from apps.assets.models import Device

    schedule = record.schedule
    device = schedule.device
    if device is None or schedule.maintenance_type != MaintenanceSchedule.MaintenanceType.CORRECTIVE:
        return False
    if record.status != MaintenanceRecord.Status.COMPLETED:
        return False
    device.refresh_from_db()
    if device.status != Device.Status.UNDER_MAINTENANCE:
        return False
    if open_corrective_jobs(device).exists():
        return False

    note = (record.notes or "").strip()
    device._transition_user = user
    device._transition_reason = f"Back in service — {schedule.title} completed" + (f": {note}" if note else "")
    device.status = Device.Status.ACTIVE
    device.save(update_fields=["status", "updated_at"])
    return True
