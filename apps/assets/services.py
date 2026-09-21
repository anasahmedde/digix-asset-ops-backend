"""What the registry does on its own once the work on an asset is finished.

The lifecycle follows the work, not a person editing a dropdown: parts are
issued, operations are completed, and the asset lands in stock by itself.
"""
from __future__ import annotations

import logging

from .models import Device, ProductionStep

logger = logging.getLogger(__name__)

# An operation nobody still has to do.
FINISHED_STEPS = (ProductionStep.Status.COMPLETED, ProductionStep.Status.SKIPPED)

# The stages a build passes through before it is finished goods.
BUILDING = (Device.Status.PROCURED, Device.Status.IN_PRODUCTION)


def build_is_complete(device: Device) -> bool:
    """True when nothing is left to do on an in-house build.

    Every component issued in full and every operation completed or skipped.
    An asset with neither components nor a route has nothing to finish, so it
    is left alone rather than declared built.
    """
    if device.source != Device.Source.INHOUSE:
        return False
    components = list(device.components.all())
    steps = list(device.production_steps.all())
    if not components and not steps:
        return False
    if any(c.outstanding_quantity > 0 for c in components):
        return False
    return all(s.status in FINISHED_STEPS for s in steps)


def finish_build_if_done(device: Device, user=None) -> bool:
    """Put a finished in-house build into stock, journalling why.

    Called wherever the last piece of work lands — the store issuing the final
    component, a technician closing the last operation, a work order being
    inspected and accepted — so the registry never waits on someone to notice.
    Returns True when the asset moved.
    """
    if device.status not in BUILDING or not build_is_complete(device):
        return False
    parts = device.components.count()
    operations = device.production_steps.count()
    device._transition_user = user
    device._transition_reason = (
        "Build complete — "
        + " and ".join(
            bit for bit in (
                f"all {parts} component{'s' if parts != 1 else ''} issued" if parts else "",
                f"all {operations} operation{'s' if operations != 1 else ''} finished" if operations else "",
            ) if bit
        )
    )
    device.status = Device.Status.IN_STOCK
    device.save(update_fields=["status", "updated_at"])
    return True
