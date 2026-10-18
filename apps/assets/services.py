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


INCREASE_RIGHT = "approve_quantity_increase"


def increase_deciders(component):
    """Who decides a requested quantity increase: whoever holds the right on
    the asker's reporting line above them - never someone below. The top of
    the line, with nobody above, decides their own. A line where nobody above
    holds the right falls back to every holder but the asker, so the request
    is never stranded."""
    from apps.assets.serializers import _project_of
    from apps.notifications.service import holders_of, line_above
    from common.permissions import can_for_project

    project = _project_of(component.device)
    asker = component.increase_requested_by
    if asker is None:
        return list(holders_of(INCREASE_RIGHT, project=project))
    above = [u for u in line_above(asker) if u.is_active and can_for_project(u, project, INCREASE_RIGHT)]
    if above:
        return above
    if asker.reports_to_id is None and can_for_project(asker, project, INCREASE_RIGHT):
        return [asker]
    return [u for u in holders_of(INCREASE_RIGHT, project=project) if u.pk != asker.pk]


def may_decide_increase(user, component) -> bool:
    """The Administrator decides anything; anyone else must be a decider."""
    from apps.accounts.models import User

    if user.is_superuser or user.role == User.Role.SUPER_ADMIN:
        return True
    return any(u.pk == user.pk for u in increase_deciders(component))
