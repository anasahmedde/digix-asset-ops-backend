"""A project's history, end to end: who did what to it, and when.

Project-level acts (opened, edited, its team, scope and requirements, the
budget submitted, approved, sent back or reopened, work orders raised) are
written here as they happen. What the other modules already record with a
name and a time is read from them rather than copied: purchase orders and
goods receipts for the project, store requests and hand-overs, every
asset's stage changes (who assigned it for installation, to which
technician) and every installation's own log (who started and completed
each step, went live, handed over).
"""
from __future__ import annotations

from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

def name(user) -> str:
    if user is None:
        return ""
    return user.get_full_name() or user.username


def log(project, action: str, text: str, *, user=None, at=None):
    """Record one act on the project."""
    from .models import ProjectActivity

    if project is None:
        return None
    actor = user if getattr(user, "is_authenticated", False) else None
    fields = {"project": project, "action": action, "text": text[:500], "actor": actor}
    if at is not None:
        fields["at"] = at
    return ProjectActivity.objects.create(**fields)


def describe(instance) -> str:
    """A side-table row (team, scope, milestone, cost, requirement) in words."""
    from .models import ProjectBOMLine, ProjectCostLine, ProjectMember, ProjectMilestone, ProjectScopeItem

    if isinstance(instance, ProjectMember):
        return f"{name(instance.user)} ({instance.get_role_display()})"
    if isinstance(instance, ProjectScopeItem):
        if instance.device_id:
            d = instance.device
            return f"asset {d.asset_code}" + (f" {d.display_name}" if d.display_name else "")
        return f"{instance.component or 'item'} x{instance.quantity}"
    if isinstance(instance, ProjectMilestone):
        return f"milestone '{instance.title}'"
    if isinstance(instance, ProjectCostLine):
        return f"cost '{instance.description or instance.cost_type}' {instance.quantity} x {instance.unit_cost}"
    if isinstance(instance, ProjectBOMLine):
        return f"requirement '{instance.description}' x{instance.quantity}"
    return str(instance)


#: Fields an edit is reported on, in the words the screen uses.
EDITED = {
    "name": "Name", "status": "Status", "phase": "Phase", "manager": "Project manager",
    "client": "Client", "site": "Site", "start_date": "Start date", "target_date": "Target date",
    "completed_date": "Completed", "contract_type": "Contract", "location": "Location",
    "client_warranty_months": "Client warranty (months)", "rental_end_date": "Rental ends",
}


def _shown(project, field):
    value = getattr(project, field)
    if field in ("status", "phase", "contract_type"):
        return getattr(project, f"get_{field}_display")()
    if field == "manager":
        return name(value) or "nobody"
    if value in (None, ""):
        return "—"
    return str(value)


def snapshot(project) -> dict:
    return {f: _shown(project, f) for f in EDITED}


def log_edit(project, before: dict, *, user):
    changes = [f"{EDITED[f]}: {before[f]} → {now}" for f, now in snapshot(project).items() if before.get(f) != now]
    if changes:
        log(project, "edited", "Edited - " + "; ".join(changes), user=user)


def _row(key, text, actor, at, action):
    return {"id": key, "action": action, "text": text, "actor": actor or None, "at": at}


def rows(project, limit: int = 500) -> list[dict]:
    """Everything that happened on the project, newest first."""
    from apps.assets.models import DeviceLifecycleEvent
    from apps.inventory.models import GoodsReceipt, IssuanceRequest
    from apps.procurement.models import PurchaseOrder
    from apps.sites.models import InstallationActivity
    from apps.workorders.models import WorkOrder

    from .costing import project_devices

    out = [
        _row(str(e.pk), e.text, name(e.actor), e.at, e.action)
        for e in project.activity.select_related("actor")[:limit]
    ]
    devices = list(project_devices(project).values_list("pk", "asset_code"))
    ids = [pk for pk, _ in devices]
    code = dict(devices)

    from apps.assets.models import Device

    stage = dict(Device.Status.choices)
    for ev in DeviceLifecycleEvent.objects.filter(device_id__in=ids).select_related("performed_by"):
        before, after = stage.get(ev.from_value, ev.from_value), stage.get(ev.to_value, ev.to_value)
        moved = (f"{before} → {after}" if before and after else after or ev.get_event_type_display())
        text = f"{code[ev.device_id]}: {moved}" + (f" - {ev.description}" if ev.description else "")
        out.append(_row(f"le-{ev.pk}", text, name(ev.performed_by), ev.created_at, "asset"))

    for ev in InstallationActivity.objects.filter(installation__device_id__in=ids).select_related("actor", "installation"):
        out.append(_row(f"in-{ev.pk}", f"{code[ev.installation.device_id]} installation: {ev.text}",
                        name(ev.actor), ev.at, "installation"))

    pos = list(
        PurchaseOrder.objects.filter(
            Q(items__bom_line__project=project)
            | Q(items__procured_devices__in=ids)
            | Q(items__asset_components__device__in=ids)
        ).distinct().select_related("supplier", "ordered_by", "approved_by")
    )
    for po in pos:
        text = f"{po.po_number} raised" + (f" with {po.supplier.name}" if po.supplier_id else "")
        if po.approved_by_id:
            text += f" - approved by {name(po.approved_by)}"
        text += f" - now {po.get_status_display()}"
        out.append(_row(f"po-{po.pk}", text, name(po.ordered_by), po.created_at, "purchase_order"))
    for grn in GoodsReceipt.objects.filter(purchase_order__in=pos).select_related("purchase_order", "received_by"):
        out.append(_row(f"grn-{grn.pk}", f"{grn.grn_number} received against {grn.purchase_order.po_number}",
                        name(grn.received_by), grn.created_at, "goods_receipt"))

    for wo in WorkOrder.objects.filter(project=project).select_related("created_by", "approved_by", "supplier"):
        text = f"{wo.wo_number} raised: {wo.title}" + (f" with {wo.supplier.name}" if wo.supplier_id else "")
        if wo.approved_by_id:
            text += f" - approved by {name(wo.approved_by)}"
        text += f" - now {wo.get_status_display()}"
        out.append(_row(f"wo-{wo.pk}", text, name(wo.created_by), wo.created_at, "work_order"))

    store = IssuanceRequest.objects.filter(
        Q(project=project) | Q(asset_component__device_id__in=ids)
    ).select_related("requested_by")
    for req in store:
        out.append(_row(f"ir-{req.pk}", f"{req.request_number}: asked the store for {req.quantity_requested} x {req.what}",
                        name(req.requested_by), req.created_at, "store_request"))
        for n, h in enumerate(req.handovers or []):
            when = h.get("at")
            if not when:
                continue
            at = parse_datetime(when)
            if at is None:
                continue
            if timezone.is_naive(at):
                at = timezone.make_aware(at)
            text = f"{req.request_number}: {h.get('quantity', '')} issued" + (
                f" to {h['received_by']}" if h.get("received_by") else "")
            out.append(_row(f"ir-{req.pk}-{n}", text, h.get("issued_by") or "", at, "store_issue"))

    out.sort(key=lambda r: r["at"], reverse=True)
    return [{**r, "at": r["at"].isoformat()} for r in out[:limit]]
