"""The project cost plan: what the build will cost before anything is bought.

Materials are priced from the assets' own components — each at the price the
company last actually paid for it, or, for an item never bought before, the
cost recorded when it was opened in inventory. Overheads are the user's own
lines. A contingency percentage sits on top. That total is what goes up for
budget approval.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Q

from .models import ProjectBudget, ProjectScopeItem

CENT = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(value or 0).quantize(CENT, rounding=ROUND_HALF_UP)


def project_devices(project):
    """Every asset on a project — through its own `project` field or a Scope
    row. Both count, or assets added from the Scope screen go missing."""
    from apps.assets.models import Device

    scoped_ids = ProjectScopeItem.objects.filter(project=project).values("device_id")
    return (
        Device.objects.filter(Q(project=project) | Q(pk__in=scoped_ids))
        .distinct()
        .order_by("asset_code")
    )


def component_unit_price(component):
    """(unit price, where it came from) for one component line.

    A price set by hand wins: the planner knows something the record does not
    — a quote, a price rise, a one-off rate. Otherwise the last procured price
    wins, since it is what the thing actually costs now, and an item that has
    never been bought falls back to the cost it was opened with. Only goods
    that were really received count as "procured" — an order that never
    arrived says nothing about the price.
    """
    from apps.procurement.models import PurchaseOrderItem

    if component.planned_unit_price is not None:
        return money(component.planned_unit_price), "Set by hand"

    if component.inventory_unit_type_id:
        lookup = {"inventory_unit_type_id": component.inventory_unit_type_id}
        opening = component.inventory_unit_type.unit_cost
    elif component.inventory_item_id:
        lookup = {"inventory_item_id": component.inventory_item_id}
        opening = component.inventory_item.unit_cost
    else:
        return None, "No inventory link"

    last = (
        # A line received at zero was never priced; it says nothing about cost.
        PurchaseOrderItem.objects.filter(received_quantity__gt=0, unit_price__gt=0, **lookup)
        .select_related("purchase_order")
        .order_by("-purchase_order__order_date", "-created_at")
        .first()
    )
    if last is not None:
        return money(last.unit_price), f"Last procured · {last.purchase_order.po_number}"
    if opening is not None and opening > 0:
        return money(opening), "Inventory opening cost"
    return None, "No price on record"


def get_or_create_plan(project):
    plan, _ = ProjectBudget.objects.get_or_create(project=project)
    return plan


def build_plan(project):
    """The whole estimate, line by line, with the totals worked out.

    Read-only: looking at a project's plan does not create one. A saved plan
    is what locks execution until approval, so merely opening the Planning tab
    on a project already under way must not freeze it.
    """
    plan = ProjectBudget.objects.filter(project=project).first()
    has_plan = plan is not None
    if plan is None:
        plan = ProjectBudget(project=project)  # unsaved — for display only

    materials = []
    unpriced = 0
    materials_total = Decimal("0")
    production_total = Decimal("0")
    devices = project_devices(project).prefetch_related(
        "components__inventory_item__material_type", "components__inventory_unit_type",
        "production_steps",
    )
    # Each asset's own material cost, so the estimate reads asset by asset —
    # including assets with nothing listed yet.
    assets = []
    for device in devices:
        asset_total = Decimal("0")
        asset_unpriced = 0
        components = list(device.components.all())
        for component in components:
            price, source = component_unit_price(component)
            line_total = money(price * component.quantity) if price is not None else None
            if line_total is None:
                unpriced += 1
                asset_unpriced += 1
            else:
                materials_total += line_total
                asset_total += line_total
            materials.append({
                "component": str(component.pk),
                "asset_code": device.asset_code,
                "asset_name": device.display_name or "",
                "name": component.name,
                "quantity": component.quantity,
                "unit_price": price,
                "price_source": source,
                "line_total": line_total,
            })
        # The build route's own cost: each operation as the planner priced it.
        steps = []
        asset_production = Decimal("0")
        for step in sorted(device.production_steps.all(), key=lambda s: s.step_number):
            cost = None if step.planned_cost is None else money(step.planned_cost)
            if cost is not None:
                asset_production += cost
            steps.append({
                "id": str(step.pk),
                "step_number": step.step_number,
                "name": step.name,
                "location": step.location,
                "workshop": step.workshop.name if step.workshop_id else (step.workshop_name or ""),
                "planned_cost": cost,
            })
        production_total += asset_production

        assets.append({
            "id": str(device.pk),
            "asset_code": device.asset_code,
            "asset_name": device.display_name or "",
            "lines": len(components),
            "materials_total": money(asset_total),
            "unpriced_lines": asset_unpriced,
            "steps": steps,
            "production_total": money(asset_production),
            "asset_total": money(asset_total + asset_production),
        })

    overheads = []
    overheads_total = Decimal("0")
    for line in project.cost_lines.all():
        amount = money(line.amount)
        overheads_total += amount
        overheads.append({
            "id": str(line.pk),
            "cost_type": line.cost_type,
            "description": line.description,
            "quantity": line.quantity,
            "unit_cost": money(line.unit_cost),
            "amount": amount,
        })

    subtotal = money(materials_total + production_total + overheads_total)
    # Contingency covers materials only: it is there for what the parts turn
    # out to cost, not for priced work or the team's own overheads.
    contingency = money(materials_total * (plan.contingency_percent or 0) / 100)
    total = money(subtotal + contingency)

    return {
        "project": str(project.pk),
        "has_plan": has_plan,
        "status": plan.status,
        "status_display": plan.get_status_display(),
        "is_editable": plan.is_editable,
        "contingency_percent": plan.contingency_percent,
        "materials": materials,
        "assets": assets,
        "overheads": overheads,
        "materials_total": money(materials_total),
        "production_total": money(production_total),
        "overheads_total": money(overheads_total),
        "subtotal": subtotal,
        "contingency_amount": contingency,
        "total": total,
        "unpriced_lines": unpriced,
        "approved_total": plan.approved_total,
        "submitted_by": _name(plan.submitted_by),
        "submitted_at": plan.submitted_at,
        "decided_by": _name(plan.decided_by),
        "decided_at": plan.decided_at,
        "decision_notes": plan.decision_notes,
        # Types already in use, so the next line can reuse the same word.
        "cost_types": sorted(
            set(project.cost_lines.values_list("cost_type", flat=True))
            | {"Travelling", "Labour", "Transport", "Accommodation"}
        ),
    }


def blocking_budget(device):
    """The project whose unapproved budget stops this asset's execution, if any.

    Execution follows approval. A project that has started a plan must have it
    signed off before it draws stock or buys; a project with no plan at all is
    left alone, so work that predates planning is not frozen.
    """
    projects = []
    if device.project_id:
        projects.append(device.project)
    projects += [
        item.project for item in ProjectScopeItem.objects.filter(device=device).select_related("project")
    ]
    for project in projects:
        plan = getattr(project, "cost_plan", None)
        if plan is not None and plan.status != ProjectBudget.Status.APPROVED:
            return project
    return None


def _name(user):
    if user is None:
        return None
    return user.get_full_name() or user.username


def component_actual(component):
    """(unit price, where it came from) for what a requirement actually cost.

    A line covered by a purchase is valued at what that purchase charged; one
    taken from stock is valued the way the warehouse values it.
    """
    po_item = component.purchase_order_item
    if po_item is not None and po_item.unit_price:
        return money(po_item.unit_price), f"Bought · {po_item.purchase_order.po_number}"
    price, source = component_unit_price(component)
    return price, (f"From stock · {source}" if price is not None else source)


def build_actuals(project):
    """What the project has actually cost so far, against what was approved."""
    plan = ProjectBudget.objects.filter(project=project).first()
    estimate = build_plan(project)

    assets = []
    materials_actual = Decimal("0")
    devices = project_devices(project).prefetch_related(
        "components__inventory_item__material_type",
        "components__inventory_unit_type",
        "components__purchase_order_item__purchase_order",
    )
    for device in devices:
        lines = []
        asset_total = Decimal("0")
        outstanding = 0
        for component in device.components.all():
            price, source = component_actual(component)
            issued = component.issued_quantity
            line_total = money(price * issued) if (price is not None and issued) else None
            if line_total:
                asset_total += line_total
            outstanding += component.outstanding_quantity
            lines.append({
                "component": str(component.pk),
                "name": component.name,
                "required": component.quantity,
                "issued": issued,
                "unit_price": price,
                "price_source": source,
                "line_total": line_total,
            })
        materials_actual += asset_total
        assets.append({
            "id": str(device.pk),
            "asset_code": device.asset_code,
            "asset_name": device.display_name or "",
            "lines": lines,
            "actual_total": money(asset_total),
            "outstanding": outstanding,
        })

    overheads = []
    planned_total = Decimal("0")
    actual_total = Decimal("0")
    for line in project.cost_lines.all():
        planned = money(line.amount)
        actual = None if line.actual_amount is None else money(line.actual_amount)
        planned_total += planned
        if actual is not None:
            actual_total += actual
        overheads.append({
            "id": str(line.pk),
            "cost_type": line.cost_type,
            "description": line.description,
            "planned_amount": planned,
            "actual_quantity": line.actual_quantity,
            "actual_unit_cost": line.actual_unit_cost,
            "actual_amount": actual,
            "unplanned": planned == 0,
        })

    total = money(materials_actual + actual_total)
    approved = plan.approved_total if plan else None
    return {
        "project": str(project.pk),
        "budget_status": plan.status if plan else None,
        "approved_total": approved,
        "estimate_total": estimate["total"],
        "assets": assets,
        "materials_actual": money(materials_actual),
        "overheads": overheads,
        "overheads_planned_total": money(planned_total),
        "overheads_actual_total": money(actual_total),
        "actual_total": total,
        # Positive means over the figure that was signed off.
        "variance_vs_approved": None if approved is None else money(total - approved),
        "cost_types": estimate["cost_types"],
    }
