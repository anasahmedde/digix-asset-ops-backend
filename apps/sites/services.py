"""Opening an installation job for an asset that has just been assigned.

An asset assigned to a technician or a vendor is an installation waiting to
happen, and the Installation Tracker is where that work is run. Raising the
job by hand afterwards means the two lists disagree until someone remembers,
so assigning opens it.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from .models import DeviceInstallation

logger = logging.getLogger(__name__)


def open_installation_for(device, user=None):
    """Put an assigned asset on the Installation Tracker.

    Idempotent: an asset reassigned mid-flight keeps the job it already has,
    complete with whatever step progress the technician has recorded. Returns
    the installation, or None when there is nothing to hang it on.
    """
    if device.current_site_id is None:
        return None

    existing = (
        DeviceInstallation.objects
        .filter(device=device, completed_at__isnull=True)
        .order_by("-installed_at")
        .first()
    )
    if existing is not None:
        # Keep the crew on the record in step with the assignment.
        updates = {}
        if existing.installed_by_id != device.assigned_technician_id:
            updates["installed_by"] = device.assigned_technician
        if device.assigned_vendor_id and existing.vendor_id != device.assigned_vendor_id:
            updates["vendor"] = device.assigned_vendor
            updates["external_vendor_name"] = ""
            updates["external_vendor_contact"] = ""
        elif not device.assigned_vendor_id and existing.external_vendor_name != device.assigned_vendor_name:
            updates["external_vendor_name"] = device.assigned_vendor_name
            updates["external_vendor_contact"] = device.assigned_vendor_contact
        if updates:
            for field, value in updates.items():
                setattr(existing, field, value)
            existing.save(update_fields=[*updates, "updated_at"])
        return existing

    try:
        return DeviceInstallation.objects.create(
            device=device,
            site=device.current_site,
            installed_by=device.assigned_technician,
            # A vendor on the register is linked; one that is not is named.
            vendor=device.assigned_vendor,
            external_vendor_name="" if device.assigned_vendor_id else device.assigned_vendor_name,
            external_vendor_contact="" if device.assigned_vendor_id else device.assigned_vendor_contact,
            # The job starts now; the steps carry their own timestamps.
            installed_at=timezone.now(),
            due_date=device.installation_date,
        )
    except Exception:  # pragma: no cover - a status change must never 500
        logger.exception("Failed to open an installation for %s", device.pk)
        return None
