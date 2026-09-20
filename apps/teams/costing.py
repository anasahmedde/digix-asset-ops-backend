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


def component_unit(component) -> str:
    """The unit of measure a component is counted in."""
    if component.unit:
        return component.unit
    if component.inventory_unit_type_id:
        return component.inventory_unit_type.unit or "piece"
    if component.inventory_item_id and component.inventory_item.material_type_id:
        return component.inventory_item.material_type.unit or "piece"
    return "piece"


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
    installation_total = Decimal("0")
    for device in devices:
        asset_total = Decimal("0")
        asset_unpriced = 0
        install = None if device.planned_installation_cost is None else money(device.planned_installation_cost)
        if install is not None:
            installation_total += install
        if device.source != device.Source.INHOUSE:
            # Bought complete from a vendor: one price, no parts list, no route.
            price = None if device.purchase_price is None else money(device.purchase_price)
            if price is None:
                unpriced += 1
            else:
                materials_total += price
            assets.append({
                "id": str(device.pk),
                "asset_code": device.asset_code,
                "asset_name": device.display_name or "",
                "vendor_asset": True,
                "source": device.source,
                "asset_price": price,
                # Nobody supplies it until the purchase order says so.
                "supply_vendor_name": (
                    device.procurement_item.purchase_order.supplier.name
                    if device.procurement_item_id and device.procurement_item.purchase_order.supplier_id
                    else None
                ),
                "po_number": (
                    device.procurement_item.purchase_order.po_number
                    if device.procurement_item_id else None
                ),
                "lines": 0,
                "materials_total": money(price or 0),
                "unpriced_lines": 0 if price is not None else 1,
                "steps": [],
                "production_total": money(0),
                "installation_cost": install,
                "asset_total": money((price or 0) + (install or 0)),
            })
            continue
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
                "unit": component_unit(component),
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
            "vendor_asset": False,
            "source": device.source,
            "asset_price": None,
            "lines": len(components),
            "materials_total": money(asset_total),
            "unpriced_lines": asset_unpriced,
            "steps": steps,
            "production_total": money(asset_production),
            "installation_cost": install,
            "asset_total": money(asset_total + asset_production + (install or 0)),
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

    subtotal = money(materials_total + production_total + installation_total + overheads_total)
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
        "installation_total": money(installation_total),
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


def step_work_order_charges(steps):
    """What the vendor charges for each operation given out, by step id.

    An operation sits on a work order either as one of its lines (a services
    order raised from Requests) or, on older single-operation orders, as the
    order itself — then the order's amount is the charge. Cancelled orders do
    not count; if more than one is live, the most recent one is taken.
    """
    from apps.workorders.models import WorkOrder, WorkOrderItem

    if not steps:
        return {}
    ids = [s.pk for s in steps]
    charges = {}
    for line in (
        WorkOrderItem.objects.filter(production_step_id__in=ids)
        .exclude(work_order__status=WorkOrder.Status.CANCELLED)
        .select_related("work_order__supplier")
        .order_by("work_order__created_at", "created_at")
    ):
        charges[line.production_step_id] = (line.work_order, money(line.line_total))
    for order in (
        WorkOrder.objects.filter(production_step_id__in=ids)
        .exclude(status=WorkOrder.Status.CANCELLED)
        .select_related("supplier")
        .order_by("created_at")
    ):
        if order.production_step_id not in charges:
            charges[order.production_step_id] = (order, money(order.total_amount))
    return charges


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
    production_actual = Decimal("0")
    work_orders_actual = Decimal("0")
    installation_actual = Decimal("0")
    devices = project_devices(project).prefetch_related(
        "components__inventory_item__material_type",
        "components__inventory_unit_type",
        "components__purchase_order_item__purchase_order",
        "production_steps", "work_orders",
    )
    for device in devices:
        lines = []
        asset_total = Decimal("0")
        outstanding = 0
        vendor_asset = device.source != device.Source.INHOUSE
        asset_priced_from = None
        if vendor_asset:
            # The complete asset costs what the purchase order charged for it,
            # once it has arrived. The order is the record of what was paid;
            # the price on the asset stands in only where no order names it.
            po_item = device.procurement_item
            paid = (
                po_item.unit_price if po_item is not None and po_item.unit_price
                else device.purchase_price
            )
            arrived = device.status != device.Status.PROCURED
            if arrived and paid is not None:
                asset_total = money(paid)
            if not arrived:
                # Still to come. An asset that has arrived but carries no price
                # is a gap in the record, not an outstanding delivery.
                outstanding = 1
            asset_priced_from = (
                f"Purchase order · {po_item.purchase_order.po_number}"
                if po_item is not None and po_item.unit_price else
                "Price on the asset" if paid is not None else "No price on record"
            )
        for component in ([] if vendor_asset else device.components.all()):
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
                "unit": component_unit(component),
                "issued": issued,
                "unit_price": price,
                "price_source": source,
                "line_total": line_total,
            })
        materials_actual += asset_total

        # The build itself: each operation as it actually cost. One given to
        # a vendor costs what its work order charges — the way a bought part
        # costs what its purchase order charged — and is not typed in. One
        # done on our own floor is recorded by hand once it is known; until
        # then it is not free, simply not known.
        steps = []
        asset_production = Decimal("0")
        device_steps = [] if vendor_asset else sorted(device.production_steps.all(), key=lambda x: x.step_number)
        charges = step_work_order_charges(device_steps)
        for step in device_steps:
            charge = charges.get(step.pk)
            if charge is not None:
                order, actual = charge
                source = f"Work order · {order.wo_number}"
                on_order = {
                    "id": str(order.pk),
                    "wo_number": order.wo_number,
                    "status": order.status,
                    "status_display": order.get_status_display(),
                    "supplier": order.supplier.name if order.supplier_id else "",
                }
            else:
                actual = None if step.actual_cost is None else money(step.actual_cost)
                source = "Recorded by hand" if actual is not None else ""
                on_order = None
            if actual is not None:
                asset_production += actual
            steps.append({
                "id": str(step.pk),
                "step_number": step.step_number,
                "name": step.name,
                "status": step.status,
                "location": step.location,
                "planned_cost": None if step.planned_cost is None else money(step.planned_cost),
                "actual_cost": actual,
                "actual_source": source,
                "actual_editable": on_order is None,
                "work_order": on_order,
            })
        production_actual += asset_production

        # A vendor-built asset costs what its work orders come to. An order
        # whose lines are this asset's operations is already counted above,
        # operation by operation, so it is not added again here.
        counted = {order.pk for order, _ in charges.values()}
        work_orders = []
        asset_work_orders = Decimal("0")
        for order in device.work_orders.all():
            if order.status == "cancelled" or order.pk in counted:
                continue
            amount = money(order.total_amount)
            asset_work_orders += amount
            work_orders.append({
                "id": str(order.pk),
                "wo_number": order.wo_number,
                "status": order.status,
                "supplier": order.supplier.name if order.supplier_id else "",
                "amount": amount,
            })
        work_orders_actual += asset_work_orders

        install = None if device.actual_installation_cost is None else money(device.actual_installation_cost)
        if install is not None:
            installation_actual += install

        assets.append({
            "id": str(device.pk),
            "asset_code": device.asset_code,
            "asset_name": device.display_name or "",
            "source": device.source,
            "vendor_asset": vendor_asset,
            "installation_actual": install,
            "installation_planned": (
                None if device.planned_installation_cost is None else money(device.planned_installation_cost)
            ),
            "asset_price": money(asset_total) if vendor_asset and asset_total else None,
            "asset_priced_from": asset_priced_from if vendor_asset else None,
            "asset_arrived": (device.status != device.Status.PROCURED) if vendor_asset else None,
            "lines": lines,
            "steps": steps,
            "work_orders": work_orders,
            "materials_actual": money(asset_total),
            "production_actual": money(asset_production),
            "work_orders_actual": money(asset_work_orders),
            "actual_total": money(asset_total + asset_production + asset_work_orders + (install or 0)),
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

    total = money(
        materials_actual + production_actual + work_orders_actual + installation_actual + actual_total
    )
    approved = plan.approved_total if plan else None
    return {
        "project": str(project.pk),
        "budget_status": plan.status if plan else None,
        "approved_total": approved,
        "estimate_total": estimate["total"],
        "assets": assets,
        "materials_actual": money(materials_actual),
        "production_actual": money(production_actual),
        "work_orders_actual": money(work_orders_actual),
        "installation_actual": money(installation_actual),
        "overheads": overheads,
        "overheads_planned_total": money(planned_total),
        "overheads_actual_total": money(actual_total),
        "actual_total": total,
        # Positive means over the figure that was signed off.
        "variance_vs_approved": None if approved is None else money(total - approved),
        "cost_types": estimate["cost_types"],
    }


def build_boq(project):
    """Bill of quantities: the same component on several assets is one line.

    A BOM is per asset; the BOQ is what the buyer works from — one row per
    component with the total quantity across every asset on the project.
    """
    devices = project_devices(project).select_related("asset_type").prefetch_related(
        "components__inventory_item__material_type", "components__inventory_unit_type",
    )
    rows = {}
    for device in devices:
        if device.source != device.Source.INHOUSE:
            # A complete asset is one line of its own on the bill.
            label = device.display_name or (device.asset_type.name if device.asset_type_id else device.asset_code)
            price = None if device.purchase_price is None else money(device.purchase_price)
            rows[("asset", device.pk)] = {
                "name": f"{label} (complete asset)",
                "unit": "asset",
                "quantity": 1,
                "unit_price": price,
                "price_source": "Vendor price" if price is not None else "No price on record",
                "assets": [f"{device.asset_code} ×1"],
            }
            continue
        for component in device.components.all():
            key = (
                ("item", component.inventory_item_id) if component.inventory_item_id
                else ("unit", component.inventory_unit_type_id) if component.inventory_unit_type_id
                else ("name", component.name.strip().lower())
            )
            row = rows.get(key)
            if row is None:
                price, source = component_unit_price(component)
                row = rows[key] = {
                    "name": component.name,
                    "unit": component.unit or (
                        component.inventory_item.material_type.unit if component.inventory_item_id
                        else component.inventory_unit_type.unit if component.inventory_unit_type_id
                        else ""
                    ),
                    "quantity": 0,
                    "unit_price": price,
                    "price_source": source,
                    "assets": [],
                }
            row["quantity"] += component.quantity
            row["assets"].append(f"{device.asset_code} ×{component.quantity}")

    lines = []
    total = Decimal("0")
    for row in sorted(rows.values(), key=lambda r: r["name"].lower()):
        amount = money(row["unit_price"] * row["quantity"]) if row["unit_price"] is not None else None
        if amount is not None:
            total += amount
        lines.append({**row, "amount": amount})
    return {"project": str(project.pk), "lines": lines, "total": money(total),
            "unpriced_lines": sum(1 for l in lines if l["amount"] is None)}
