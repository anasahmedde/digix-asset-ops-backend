"""
The report catalogue. Each report is registered with what it is called, where
it sits, what it is for and the right a person needs to run it, and its
builder returns::

    {columns: [{key, label, kind, total?}], rows: [...],
     kpis: [{label, value, kind, tone?}], breakdown: {title, items: [{label, value}]}}

``kind`` drives formatting on screen and in Excel (see ``xlsx.py``). Money
columns appear only for someone allowed to see the figures. Reports stay a
thin read layer over the domain apps — no models of their own.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from common.permissions import can

ROW_LIMIT = 5000
REPORTS: dict[str, dict] = {}


def report(key, *, title, group, description, capability, filters=("date",), date_label="Created", statuses=()):
    def register(fn):
        REPORTS[key] = {
            "key": key, "title": title, "group": group, "description": description,
            "capability": capability, "filters": list(filters), "date_label": date_label,
            # "app.Model": its Status choices, read when the catalogue is.
            "statuses": statuses, "build": fn,
        }
        return fn
    return register


def status_options(meta):
    from django.apps import apps

    if not meta["statuses"]:
        return []
    model = apps.get_model(meta["statuses"])
    return [{"value": v, "label": str(label)} for v, label in model.Status.choices]


def col(key, label, kind="text", total=None):
    c = {"key": key, "label": label, "kind": kind}
    if total is not None:
        c["total"] = total
    return c


def kpi(label, value, kind="int", tone=None):
    return {"label": label, "value": value, "kind": kind, "tone": tone}


def breakdown(title, counter: Counter):
    return {"title": title, "items": [{"label": k, "value": v} for k, v in counter.most_common()]}


def _dated(qs, f, field="created_at", is_date=False):
    suffix = "" if is_date else "__date"
    if f.get("date_from"):
        qs = qs.filter(**{f"{field}{suffix}__gte": f["date_from"]})
    if f.get("date_to"):
        qs = qs.filter(**{f"{field}{suffix}__lte": f["date_to"]})
    return qs


def _name(user):
    return (user.get_full_name() or user.username) if user is not None else ""


def _money(v):
    return None if v is None else Decimal(v)


# ── Assets ───────────────────────────────────────────────────────────────

@report("assets", title="Asset Register", group="Assets", capability="view_assets",
        description="Every asset with its type, source, status, project and where it stands, and when it went in.",
        filters=("date", "status", "project", "site"), date_label="Registered",
        statuses="assets.Device")
def assets_report(request, f):
    from apps.assets.models import Device

    qs = _dated(Device.objects.select_related(
        "asset_type", "device_model", "current_site", "assigned_client", "project",
    ), f)
    if f.get("status"):
        qs = qs.filter(status=f["status"])
    if f.get("project"):
        qs = qs.filter(Q(project_id=f["project"]) | Q(project_scope_items__project_id=f["project"])).distinct()
    if f.get("site"):
        qs = qs.filter(current_site_id=f["site"])
    prices = can(request.user, "view_prices")
    devices = list(qs.order_by("asset_code")[:ROW_LIMIT])
    rows = [{
        "asset_code": d.asset_code,
        "name": d.display_name,
        "type": d.asset_type.name if d.asset_type_id else "",
        "model": str(d.device_model) if d.device_model_id else "",
        "serial": d.serial_number,
        "source": d.get_source_display(),
        "status": d.get_status_display(),
        "project": d.project.name if d.project_id else "",
        "site": d.current_site.name if d.current_site_id else "",
        "city": d.current_site.city if d.current_site_id else "",
        "client": d.assigned_client.name if d.assigned_client_id else "",
        "installed": d.installation_date,
        "registered": d.created_at,
        "price": d.purchase_price,
    } for d in devices]
    statuses = Counter(d.get_status_display() for d in devices)
    live = sum(1 for d in devices if d.status in ("installed", "active", "client_property"))
    columns = [
        col("asset_code", "Asset ID"), col("name", "Asset name"), col("type", "Type"), col("model", "Model"),
        col("serial", "Serial no."), col("source", "Source"), col("status", "Status"), col("project", "Project"),
        col("site", "Site"), col("city", "City"), col("client", "Client"),
        col("installed", "Installed on", "date"), col("registered", "Registered", "date"),
    ]
    if prices:
        columns.append(col("price", "Purchase price", "money"))
    return {
        "columns": columns, "rows": rows,
        "kpis": [
            kpi("Assets", len(devices)),
            kpi("Installed / live", live, tone="good"),
            kpi("Being built or in stock", sum(1 for d in devices if d.status in ("procured", "in_production", "in_stock", "assigned"))),
            kpi("Under maintenance", sum(1 for d in devices if d.status == "under_maintenance"), tone="warn"),
        ] + ([kpi("Asset value", sum((d.purchase_price or 0) for d in devices), "money")] if prices else []),
        "breakdown": breakdown("Assets by status", statuses),
    }


@report("installations", title="Installation Register", group="Assets", capability="view_installations",
        description="Where each asset was installed: site, crew, booked and due dates, first check-in, completion and handover.",
        filters=("date", "project", "site"), date_label="Booked")
def installations_report(request, f):
    from apps.sites.activity import presence_rows
    from apps.sites.models import DeviceInstallation
    from apps.sites.serializers import _health

    qs = _dated(DeviceInstallation.objects.select_related(
        "device", "device__project", "site", "zone", "installed_by", "vendor", "handover",
    ).prefetch_related("helpers"), f, "installed_at")
    if f.get("project"):
        qs = qs.filter(Q(device__project_id=f["project"]) | Q(device__project_scope_items__project_id=f["project"])).distinct()
    if f.get("site"):
        qs = qs.filter(site_id=f["site"])
    jobs = list(qs.order_by("-installed_at")[:ROW_LIMIT])
    rows, health = [], Counter()
    for inst in jobs:
        key, label, _ = _health(inst)
        health[label] += 1
        arrived = [p["_at"] for p in presence_rows(inst) if p["action"] == "checked_in"]
        record = getattr(inst, "handover", None)
        rows.append({
            "asset_code": inst.device.asset_code,
            "asset": inst.device.display_name,
            "project": inst.device.project.name if inst.device.project_id else "",
            "site": inst.site.name,
            "city": inst.site.city,
            "position": inst.position_label or (inst.zone.name if inst.zone_id else ""),
            "installer": _name(inst.installed_by) or (inst.vendor.name if inst.vendor_id else inst.external_vendor_name),
            "helpers": ", ".join(_name(h) for h in inst.helpers.all()),
            "booked": inst.installed_at,
            "due": inst.due_date,
            "first_check_in": min(arrived) if arrived else None,
            "completed": inst.completed_at,
            "handover": record.handover_date if record else None,
            "accepted_by": record.accepted_by_name if record else "",
            "status": "Removed" if inst.removed_at else label,
        })
    return {
        "columns": [
            col("asset_code", "Asset ID"), col("asset", "Asset name"), col("project", "Project"),
            col("site", "Site"), col("city", "City"), col("position", "Position at site"),
            col("installer", "Installer"), col("helpers", "Helpers"),
            col("booked", "Booked on", "date"), col("due", "Due", "date"),
            col("first_check_in", "First check-in", "datetime"), col("completed", "Completed", "datetime"),
            col("handover", "Handed over", "date"), col("accepted_by", "Accepted by"), col("status", "Status"),
        ],
        "rows": rows,
        "kpis": [
            kpi("Installations", len(jobs)),
            kpi("Completed", sum(1 for i in jobs if i.completed_at), tone="good"),
            kpi("Handed over", sum(1 for r in rows if r["handover"]), tone="good"),
            kpi("Overdue", health.get("Overdue", 0) + health.get("Delayed", 0), tone="bad"),
        ],
        "breakdown": breakdown("Installations by status", health),
    }


# ── Projects ─────────────────────────────────────────────────────────────

@report("projects", title="Project Portfolio", group="Projects", capability="view_projects",
        description="Every project with its phase, health, progress and dates — and budget against actual cost.",
        filters=("date", "status"), date_label="Created",
        statuses="teams.Project")
def projects_report(request, f):
    from apps.teams.costing import build_actuals, project_devices
    from apps.teams.models import Project

    qs = _dated(Project.objects.select_related("client", "manager").prefetch_related("sites"), f)
    if f.get("status"):
        qs = qs.filter(status=f["status"])
    costs = can(request.user, "view_project_costs")
    labels = dict(Project.Status.choices)
    rows, health = [], Counter()
    projects = list(qs.order_by("-created_at")[:500])
    for p in projects:
        progress = p.computed_progress()
        state = labels.get(p.health(progress), p.get_status_display())
        health[state] += 1
        row = {
            "code": p.project_code, "name": p.name,
            "client": p.client.name if p.client_id else "",
            "sites": ", ".join(s.name for s in p.sites.all()),
            "manager": _name(p.manager), "contract": p.get_contract_type_display() if p.contract_type else "",
            "phase": p.get_phase_display(), "status": state, "progress": progress,
            "start": p.start_date, "closing": p.target_date, "completed": p.completed_date,
            "assets": project_devices(p).count(),
        }
        if costs:
            a = build_actuals(p)
            approved = _money(a["approved_total"])
            actual = _money(a["actual_total"])
            row.update({
                "approved": approved, "estimate": _money(a["estimate_total"]), "actual": actual,
                "variance": None if approved is None else actual - approved,
                "used": None if not approved else round(float(actual / approved * 100), 1),
            })
        rows.append(row)
    columns = [
        col("code", "Project ID"), col("name", "Project"), col("client", "Client"), col("sites", "Sites"),
        col("manager", "Manager"), col("contract", "Contract"), col("phase", "Phase"), col("status", "Health"),
        col("progress", "Progress", "percent"), col("start", "Start", "date"), col("closing", "Closing date", "date"),
        col("completed", "Completed", "date"), col("assets", "Assets", "int", total=True),
    ]
    if costs:
        columns += [
            col("approved", "Approved budget", "money"), col("estimate", "Estimate", "money"),
            col("actual", "Actual cost", "money"), col("variance", "Variance (over +)", "variance"),
            col("used", "Budget used", "percent"),
        ]
    kpis = [
        kpi("Projects", len(rows)),
        kpi("On track", health.get("On Track", 0), tone="good"),
        kpi("At risk", health.get("At Risk", 0), tone="warn"),
        kpi("Delayed", health.get("Delayed", 0), tone="bad"),
    ]
    if costs:
        kpis += [
            kpi("Approved budgets", sum((r["approved"] or 0) for r in rows), "money"),
            kpi("Actual cost", sum((r["actual"] or 0) for r in rows), "money"),
        ]
    return {"columns": columns, "rows": rows, "kpis": kpis, "breakdown": breakdown("Projects by health", health)}


# ── Maintenance & service ────────────────────────────────────────────────

@report("visits", title="Maintenance Visits", group="Maintenance & Service", capability="view_maintenance",
        description="Each maintenance visit — who went, where, how long on site, and the outcome. Can be rolled up.",
        filters=("date", "technician", "site", "project", "group_by"), date_label="Visit date")
def visits_report(request, f):
    from apps.maintenance.models import MaintenanceVisit

    qs = MaintenanceVisit.objects.select_related(
        "schedule", "schedule__site", "schedule__device", "schedule__device__project",
        "schedule__ticket", "assigned_to",
    ).exclude(status=MaintenanceVisit.Status.SKIPPED)
    if f.get("date_from"):
        qs = qs.filter(Q(started_at__date__gte=f["date_from"]) | Q(started_at__isnull=True, due_date__gte=f["date_from"]))
    if f.get("date_to"):
        qs = qs.filter(Q(started_at__date__lte=f["date_to"]) | Q(started_at__isnull=True, due_date__lte=f["date_to"]))
    if f.get("technician"):
        qs = qs.filter(assigned_to_id=f["technician"])
    if f.get("site"):
        qs = qs.filter(schedule__site_id=f["site"])
    if f.get("project"):
        qs = qs.filter(schedule__device__project_id=f["project"])
    if f.get("status"):
        qs = qs.filter(status=f["status"])

    def hours(v):
        if v.started_at and v.completed_at:
            return round((v.completed_at - v.started_at).total_seconds() / 3600, 2)
        return None

    def name_of(v, what):
        s = v.schedule
        if what == "technician":
            return _name(v.assigned_to) if v.assigned_to_id else "Unassigned"
        if what == "site":
            return s.site.name if s.site_id else "No site"
        return s.device.project.name if s.device_id and s.device.project_id else "No project"

    visits = list(qs.order_by("-due_date")[:ROW_LIMIT])
    timed = [h for h in (hours(v) for v in visits) if h is not None]
    kpis = [
        kpi("Visits", len(visits)),
        kpi("Completed", sum(1 for v in visits if v.status == "completed"), tone="good"),
        kpi("Sent back", sum(1 for v in visits if v.review_decision == MaintenanceVisit.Review.UNRESOLVED), tone="bad"),
        kpi("Avg hours on site", round(sum(timed) / len(timed), 1) if timed else None, "number"),
    ]
    statuses = Counter(v.get_status_display() for v in visits)
    group_by = f.get("group_by") or ""
    if group_by in ("technician", "site", "project"):
        buckets = {}
        for v in visits:
            b = buckets.setdefault(name_of(v, group_by), {
                group_by: name_of(v, group_by), "visits": 0, "completed": 0, "in_progress": 0,
                "awaiting_review": 0, "planned": 0, "unresolved": 0, "hours": 0.0, "_timed": 0,
            })
            b["visits"] += 1
            b[v.status] = b.get(v.status, 0) + 1
            if v.review_decision == MaintenanceVisit.Review.UNRESOLVED:
                b["unresolved"] += 1
            h = hours(v)
            if h is not None:
                b["hours"] += h
                b["_timed"] += 1
        rows = []
        for b in sorted(buckets.values(), key=lambda r: -r["visits"]):
            n = b.pop("_timed")
            b["hours"] = round(b["hours"], 1)
            b["avg_hours"] = round(b["hours"] / n, 1) if n else None
            rows.append(b)
        return {
            "columns": [
                col(group_by, group_by.title()), col("visits", "Visits", "int", True),
                col("completed", "Completed", "int", True), col("in_progress", "In progress", "int", True),
                col("awaiting_review", "Awaiting review", "int", True), col("planned", "Planned", "int", True),
                col("unresolved", "Sent back", "int", True), col("hours", "Hours on site", "number", True),
                col("avg_hours", "Avg hours / visit", "number"),
            ],
            "rows": rows, "kpis": kpis, "breakdown": breakdown("Visits by status", statuses),
        }

    rows = [{
        "technician": name_of(v, "technician"),
        "site": name_of(v, "site"),
        "project": name_of(v, "project"),
        "asset": v.schedule.device.asset_code if v.schedule.device_id else "",
        "job": v.schedule.ticket.ticket_number if v.schedule.ticket_id and v.schedule.ticket.ticket_number else v.schedule.title,
        "type": v.schedule.get_maintenance_type_display(),
        "visit": v.sequence,
        "status": v.get_status_display(),
        "due_date": v.due_date,
        "started_at": v.started_at,
        "completed_at": v.completed_at,
        "hours": hours(v),
        "asset_verified": "Yes" if v.asset_verified_at else "No",
        "outcome": v.get_review_decision_display() if v.review_decision else ("Fixed" if v.resolved else "Not fixed" if v.resolved is False else ""),
    } for v in visits]
    return {
        "columns": [
            col("technician", "Technician"), col("site", "Site"), col("project", "Project"),
            col("asset", "Asset"), col("job", "Job"), col("type", "Type"), col("visit", "Visit #", "int"),
            col("status", "Status"), col("due_date", "Due", "date"), col("started_at", "Started", "datetime"),
            col("completed_at", "Finished", "datetime"), col("hours", "Hours", "number", True),
            col("asset_verified", "Asset scanned"), col("outcome", "Outcome"),
        ],
        "rows": rows, "kpis": kpis, "breakdown": breakdown("Visits by status", statuses),
    }


@report("tickets", title="Service Tickets", group="Maintenance & Service", capability="view_tickets",
        description="Tickets with priority, who has them, due dates, resolution time and whether they ran late.",
        filters=("date", "status"), date_label="Reported",
        statuses="tickets.Ticket")
def tickets_report(request, f):
    from apps.tickets.models import Ticket

    qs = _dated(Ticket.objects.select_related("site", "assigned_to", "device"), f)
    for key in ("status", "priority", "category"):
        if f.get(key):
            qs = qs.filter(**{key: f[key]})
    today = timezone.localdate()
    done = ("closed", "cancelled", "approved", "rejected")
    rows = []
    for t in qs.order_by("-created_at")[:ROW_LIMIT]:
        finished = t.resolved_at or t.closed_at
        late = bool(t.due_date and ((finished and finished.date() > t.due_date) or (not finished and t.status not in done and today > t.due_date)))
        rows.append({
            "id": t.ticket_number or str(t.id)[:8], "title": t.title,
            "priority": t.get_priority_display(), "category": t.get_category_display(),
            "status": t.get_status_display(), "site": t.site.name if t.site_id else "",
            "asset": t.device.asset_code if t.device_id else "",
            "assigned_to": _name(t.assigned_to) if t.assigned_to_id else "",
            "reported": t.created_at, "due": t.due_date, "resolved": finished,
            "hours": round((finished - t.created_at).total_seconds() / 3600, 1) if finished else None,
            "late": "Yes" if late else "No", "_open": t.status not in done,
        })
    resolved = [r["hours"] for r in rows if r["hours"] is not None]
    return {
        "columns": [
            col("id", "Ticket"), col("title", "Title"), col("priority", "Priority"), col("category", "Category"),
            col("status", "Status"), col("site", "Site"), col("asset", "Asset"), col("assigned_to", "Assigned to"),
            col("reported", "Reported", "datetime"), col("due", "Due", "date"), col("resolved", "Resolved", "datetime"),
            col("hours", "Hours to resolve", "number"), col("late", "Late"),
        ],
        "rows": rows,
        "kpis": [
            kpi("Tickets", len(rows)),
            kpi("Open", sum(1 for r in rows if r["_open"]), tone="warn"),
            kpi("Late", sum(1 for r in rows if r["late"] == "Yes"), tone="bad"),
            kpi("Avg hours to resolve", round(sum(resolved) / len(resolved), 1) if resolved else None, "number"),
        ],
        "breakdown": breakdown("Tickets by status", Counter(r["status"] for r in rows)),
    }


# ── Procurement & stock ──────────────────────────────────────────────────

@report("purchase_orders", title="Purchase Orders", group="Procurement & Stock", capability="view_procurement",
        description="Orders placed with suppliers: status, order and delivery dates, and value.",
        filters=("date", "status"), date_label="Raised",
        statuses="procurement.PurchaseOrder")
def purchase_orders_report(request, f):
    from apps.procurement.models import PurchaseOrder

    qs = _dated(PurchaseOrder.objects.select_related("supplier"), f)
    if f.get("status"):
        qs = qs.filter(status=f["status"])
    prices = can(request.user, "view_prices")
    orders = list(qs.order_by("-created_at")[:ROW_LIMIT])
    today = timezone.localdate()
    open_states = ("approved", "ordered", "partially_received")
    rows = [{
        "po": o.po_number, "supplier": o.supplier.name if o.supplier_id else "", "status": o.get_status_display(),
        "raised": o.created_at, "ordered": o.order_date, "expected": o.expected_delivery,
        "late": "Yes" if o.status in open_states and o.expected_delivery and o.expected_delivery < today else "",
        "currency": o.currency, "total": o.total_amount,
    } for o in orders]
    columns = [
        col("po", "PO number"), col("supplier", "Supplier"), col("status", "Status"), col("raised", "Raised", "date"),
        col("ordered", "Ordered", "date"), col("expected", "Expected delivery", "date"), col("late", "Overdue"),
    ]
    if prices:
        columns += [col("currency", "Currency"), col("total", "Order value", "money")]
    kpis = [
        kpi("Orders", len(rows)),
        kpi("Open", sum(1 for o in orders if o.status in open_states), tone="warn"),
        kpi("Overdue deliveries", sum(1 for r in rows if r["late"]), tone="bad"),
    ]
    if prices:
        kpis.append(kpi("Order value", sum((o.total_amount or 0) for o in orders if o.status != "cancelled"), "money"))
    return {"columns": columns, "rows": rows, "kpis": kpis, "breakdown": breakdown("Orders by status", Counter(r["status"] for r in rows))}


@report("work_orders", title="Work Orders", group="Procurement & Stock", capability="view_work_orders",
        description="Work given to vendors and workshops: what, for which project, status, delivery and value.",
        filters=("date", "status"), date_label="Raised",
        statuses="workorders.WorkOrder")
def work_orders_report(request, f):
    from apps.workorders.models import WorkOrder

    qs = _dated(WorkOrder.objects.select_related("supplier", "project", "device"), f)
    if f.get("status"):
        qs = qs.filter(status=f["status"])
    prices = can(request.user, "view_prices")
    orders = list(qs.order_by("-created_at")[:ROW_LIMIT])
    rows = [{
        "wo": w.wo_number, "title": w.title, "type": w.get_order_type_display(), "status": w.get_status_display(),
        "supplier": w.supplier.name if w.supplier_id else "", "project": w.project.name if w.project_id else "",
        "asset": w.device.asset_code if w.device_id else "", "raised": w.created_at,
        "expected": w.expected_delivery, "currency": w.currency, "total": w.total_amount,
    } for w in orders]
    columns = [
        col("wo", "WO number"), col("title", "Title"), col("type", "Type"), col("status", "Status"),
        col("supplier", "Vendor"), col("project", "Project"), col("asset", "Asset"),
        col("raised", "Raised", "date"), col("expected", "Expected delivery", "date"),
    ]
    if prices:
        columns += [col("currency", "Currency"), col("total", "Value", "money")]
    kpis = [
        kpi("Work orders", len(rows)),
        kpi("Open", sum(1 for w in orders if w.status not in ("completed", "cancelled", "delivered")), tone="warn"),
    ]
    if prices:
        kpis.append(kpi("Value", sum((w.total_amount or 0) for w in orders if w.status != "cancelled"), "money"))
    return {"columns": columns, "rows": rows, "kpis": kpis, "breakdown": breakdown("Work orders by status", Counter(r["status"] for r in rows))}


@report("inventory", title="Stock on Hand", group="Procurement & Stock", capability="view_stock",
        description="Generic stock by item: quantity against minimum level, where it is kept and what it is worth.",
        filters=("low_stock",), date_label="")
def inventory_report(request, f):
    from apps.inventory.models import InventoryItem

    qs = InventoryItem.objects.select_related("material_type", "category")
    prices = can(request.user, "view_prices")
    items = list(qs.order_by("sku")[:ROW_LIMIT])
    if f.get("low_stock") == "true":
        items = [i for i in items if i.is_low_stock]
    rows = []
    for i in items:
        state = "Out of stock" if i.quantity <= 0 else "Low" if i.is_low_stock else "OK"
        rows.append({
            "sku": i.sku, "item": i.material_type.name if i.material_type_id else "",
            "category": i.category.name if i.category_id else "",
            "unit": (i.material_type.unit if i.material_type_id else "") or "piece",
            "where": i.storage_location or i.get_location_display(),
            "quantity": i.quantity, "min": i.min_stock_level, "state": state,
            "unit_cost": i.unit_cost,
            "value": None if i.unit_cost is None else Decimal(i.unit_cost) * i.quantity,
        })
    columns = [
        col("sku", "SKU"), col("item", "Item"), col("category", "Category"), col("unit", "Unit"),
        col("where", "Stored at"), col("quantity", "On hand", "int"), col("min", "Min level", "int"), col("state", "Stock"),
    ]
    if prices:
        columns += [col("unit_cost", "Unit cost", "money", False), col("value", "Stock value", "money")]
    kpis = [
        kpi("Items", len(rows)),
        kpi("Low", sum(1 for r in rows if r["state"] == "Low"), tone="warn"),
        kpi("Out of stock", sum(1 for r in rows if r["state"] == "Out of stock"), tone="bad"),
    ]
    if prices:
        kpis.append(kpi("Stock value", sum((r["value"] or 0) for r in rows), "money"))
    return {"columns": columns, "rows": rows, "kpis": kpis, "breakdown": breakdown("Items by stock level", Counter(r["state"] for r in rows))}


# ── Partners ─────────────────────────────────────────────────────────────

@report("suppliers", title="Supplier Directory", group="Partners", capability="view_suppliers",
        description="Suppliers and vendors with the services they provide and who to call.", date_label="Added")
def suppliers_report(request, f):
    from apps.suppliers.models import Supplier

    rows = [{
        "code": s.code, "name": s.name, "categories": ", ".join(c.name for c in s.service_categories.all()),
        "contact_person": s.contact_person, "contact_phone": s.contact_phone,
        "active": "Active" if s.is_active else "Inactive",
    } for s in _dated(Supplier.objects.prefetch_related("service_categories"), f).order_by("name")[:ROW_LIMIT]]
    return {
        "columns": [
            col("code", "Code"), col("name", "Supplier"), col("categories", "Services"),
            col("contact_person", "Contact"), col("contact_phone", "Phone"), col("active", "Status"),
        ],
        "rows": rows,
        "kpis": [kpi("Suppliers", len(rows)), kpi("Active", sum(1 for r in rows if r["active"] == "Active"), tone="good")],
        "breakdown": None,
    }


@report("clients", title="Client Directory", group="Partners", capability="view_clients",
        description="Clients with their contacts and how many projects each has.", date_label="Added")
def clients_report(request, f):
    from django.db.models import Count

    from apps.clients.models import Client

    rows = [{
        "code": c.code, "name": c.name, "contact_person": c.contact_person,
        "contact_phone": c.contact_phone, "contact_email": c.contact_email,
        "projects": c.n_projects, "active": "Active" if c.is_active else "Inactive",
    } for c in _dated(Client.objects.annotate(n_projects=Count("projects")), f).order_by("name")[:ROW_LIMIT]]
    return {
        "columns": [
            col("code", "Code"), col("name", "Client"), col("contact_person", "Contact"),
            col("contact_phone", "Phone"), col("contact_email", "Email"),
            col("projects", "Projects", "int", True), col("active", "Status"),
        ],
        "rows": rows,
        "kpis": [kpi("Clients", len(rows)), kpi("Projects", sum(r["projects"] for r in rows))],
        "breakdown": None,
    }


# Older links asked for "teams".
ALIASES = {"teams": "projects"}
