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


def return_leftovers(device, *, user, rows):
    """Parts issued for this asset's build and not used go back to the store.

    Only once the production route is finished. Each row is
    ``{"component": id, "quantity": n, "serials": [...]}``; a unique part
    comes back by serial, and only a serial that was issued for this line.
    The parts wait in Receiving, like a maintenance leftover, under one
    return receipt. Returns the receipt.
    """
    from rest_framework import serializers

    from apps.inventory.models import GoodsReceipt, IssuanceRequest
    from apps.inventory.returns import record_return

    from .models import DeviceLifecycleEvent

    unfinished = device.production_steps.exclude(status__in=FINISHED_STEPS)
    if unfinished.exists():
        raise serializers.ValidationError({"detail": (
            "Leftovers go back once the production route is finished - "
            f"{unfinished.order_by('step_number').first().name} is not done yet."
        )})
    if not rows:
        raise serializers.ValidationError({"rows": ["Say what is coming back."]})
    lines = {str(c.pk): c for c in device.components.select_related("inventory_item", "inventory_unit_type")}
    coming_back, touched = [], []
    for row in rows:
        line = lines.get(str(row.get("component")))
        if line is None:
            raise serializers.ValidationError({"rows": ["That component is not on this asset."]})
        try:
            quantity = int(row.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        if quantity < 1:
            continue
        left = line.issued_quantity - line.returned_quantity
        if quantity > left:
            raise serializers.ValidationError({"rows": [
                f"{line.name}: {line.issued_quantity} issued and {line.returned_quantity} already back - "
                f"at most {max(left, 0)} can be returned."
            ]})
        serials = [str(x).strip() for x in (row.get("serials") or []) if str(x).strip()]
        if line.inventory_unit_type_id:
            issued = [s for ir in IssuanceRequest.objects.filter(asset_component=line) for s in (ir.issued_serials or [])]
            if len(serials) != quantity or len(set(serials)) != quantity:
                raise serializers.ValidationError({"rows": [
                    f"{line.name}: pick the {quantity} unit(s) coming back by serial number."
                ]})
            stray = [s for s in serials if s not in issued or s in (line.returned_serials or [])]
            if stray:
                raise serializers.ValidationError({"rows": [
                    f"{line.name}: {', '.join(stray)} was not issued for this asset, or is back already."
                ]})
        coming_back.append({
            "item": line.inventory_item_id, "unit_type": line.inventory_unit_type_id,
            "quantity": quantity, "serials": serials,
        })
        touched.append((line, quantity, serials))
    if not coming_back:
        raise serializers.ValidationError({"rows": ["Return at least one."]})

    project = device.project_on
    receipt = record_return(
        source=GoodsReceipt.Source.PROJECT_RETURN,
        rows=coming_back,
        reference=" · ".join(x for x in [device.asset_code, project.name if project else ""] if x),
        notes="Left over from production.",
        user=user,
    )
    for line, quantity, serials in touched:
        line.returned_quantity += quantity
        line.returned_serials = list(line.returned_serials or []) + serials
        line.save(update_fields=["returned_quantity", "returned_serials", "updated_at"])
    what = ", ".join(f"{q} x {line.name}" + (f" ({', '.join(s)})" if s else "") for line, q, s in touched)
    DeviceLifecycleEvent.objects.create(
        device=device, event_type=DeviceLifecycleEvent.EventType.NOTE,
        description=f"Production leftovers back to the store on {receipt.grn_number}: {what}",
        performed_by=user,
        metadata={"return_grn": receipt.grn_number},
    )
    if project is not None:
        from apps.teams import activity as project_log

        project_log.log(project, "returned", f"{device.asset_code}: production leftovers returned - {what} ({receipt.grn_number})", user=user)
    return receipt
