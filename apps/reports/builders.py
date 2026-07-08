"""
Report builders — one function per report type. Each returns a dict:
``{type, columns:[{key,label}], rows:[...], count, summary}``.

Kept as plain query functions (no models) so reports stay a thin read layer
over the existing domain apps.
"""

from __future__ import annotations

from django.db.models import Count, Sum


def _date_filter(qs, date_from, date_to, field="created_at"):
    if date_from:
        qs = qs.filter(**{f"{field}__date__gte": date_from})
    if date_to:
        qs = qs.filter(**{f"{field}__date__lte": date_to})
    return qs


def _cols(*pairs):
    return [{"key": k, "label": l} for k, l in pairs]


def assets_report(request, date_from, date_to):
    from apps.assets.models import Device

    qs = _date_filter(
        Device.objects.select_related("asset_type", "device_model", "current_site", "assigned_client"),
        date_from, date_to,
    )
    status = request.query_params.get("status")
    if status:
        qs = qs.filter(status=status)

    rows = [{
        "asset_code": d.asset_code,
        "type": d.asset_type.name if d.asset_type_id else "",
        "model": str(d.device_model) if d.device_model_id else "",
        "status": d.get_status_display(),
        "site": d.current_site.name if d.current_site_id else "",
        "client": d.assigned_client.name if d.assigned_client_id else "",
        "installation_date": d.installation_date,
    } for d in qs[:5000]]

    by_status = dict(qs.values_list("status").annotate(c=Count("id")))
    return {
        "type": "assets",
        "columns": _cols(
            ("asset_code", "Asset Code"), ("type", "Type"), ("model", "Model"),
            ("status", "Status"), ("site", "Site"), ("client", "Client"),
            ("installation_date", "Installed"),
        ),
        "rows": rows, "count": len(rows),
        "summary": {"total": qs.count(), "by_status": by_status},
    }


def tickets_report(request, date_from, date_to):
    from apps.tickets.models import Ticket

    qs = _date_filter(Ticket.objects.select_related("site", "assigned_to", "device"), date_from, date_to)
    for f in ("status", "priority", "category"):
        v = request.query_params.get(f)
        if v:
            qs = qs.filter(**{f: v})

    rows = [{
        "id": t.ticket_number or str(t.id)[:8],
        "title": t.title,
        "status": t.get_status_display(),
        "priority": t.get_priority_display(),
        "category": t.get_category_display(),
        "site": t.site.name if t.site_id else "",
        "assigned_to": t.assigned_to.get_full_name() if t.assigned_to_id else "",
        "created_at": t.created_at.date(),
    } for t in qs[:5000]]

    return {
        "type": "tickets",
        "columns": _cols(
            ("id", "Ticket"), ("title", "Title"), ("status", "Status"),
            ("priority", "Priority"), ("category", "Category"), ("site", "Site"),
            ("assigned_to", "Assigned To"), ("created_at", "Created"),
        ),
        "rows": rows, "count": len(rows),
        "summary": {
            "total": qs.count(),
            "by_status": dict(qs.values_list("status").annotate(c=Count("id"))),
        },
    }


def work_orders_report(request, date_from, date_to):
    from apps.workorders.models import WorkOrder

    qs = _date_filter(WorkOrder.objects.select_related("supplier", "client"), date_from, date_to)
    status = request.query_params.get("status")
    if status:
        qs = qs.filter(status=status)

    rows = [{
        "wo_number": w.wo_number,
        "title": w.title,
        "type": w.get_order_type_display(),
        "status": w.get_status_display(),
        "supplier": w.supplier.name if w.supplier_id else "",
        "total": f"{w.currency} {w.total_amount:,.2f}",
        "expected_delivery": w.expected_delivery,
    } for w in qs[:5000]]

    total_value = qs.aggregate(s=Sum("total_amount"))["s"] or 0
    return {
        "type": "work_orders",
        "columns": _cols(
            ("wo_number", "WO #"), ("title", "Title"), ("type", "Type"),
            ("status", "Status"), ("supplier", "Supplier"), ("total", "Total"),
            ("expected_delivery", "Delivery"),
        ),
        "rows": rows, "count": len(rows),
        "summary": {"total": qs.count(), "total_value": float(total_value)},
    }


def inventory_report(request, date_from, date_to):
    from apps.inventory.models import InventoryItem

    qs = InventoryItem.objects.select_related("material_type", "category").all()
    category = request.query_params.get("category")
    if category:
        qs = qs.filter(category_id=category)
    if request.query_params.get("low_stock") == "true":
        qs = [i for i in qs if i.is_low_stock]

    rows = [{
        "sku": i.sku,
        "material": i.material_type.name if i.material_type_id else "",
        "category": i.category.name if i.category_id else "",
        "location": i.get_location_display(),
        "quantity": i.quantity,
        "min_stock": i.min_stock_level,
        "low_stock": "Yes" if i.is_low_stock else "No",
    } for i in list(qs)[:5000]]

    return {
        "type": "inventory",
        "columns": _cols(
            ("sku", "SKU"), ("material", "Material"), ("category", "Category"),
            ("location", "Location"), ("quantity", "Qty"), ("min_stock", "Min"),
            ("low_stock", "Low Stock"),
        ),
        "rows": rows, "count": len(rows),
        "summary": {"total": len(rows), "low_stock": sum(1 for r in rows if r["low_stock"] == "Yes")},
    }


def suppliers_report(request, date_from, date_to):
    from apps.suppliers.models import Supplier

    qs = _date_filter(Supplier.objects.prefetch_related("service_categories"), date_from, date_to)
    rows = [{
        "code": s.code,
        "name": s.name,
        "categories": ", ".join(c.name for c in s.service_categories.all()),
        "contact_person": s.contact_person,
        "contact_phone": s.contact_phone,
        "active": "Yes" if s.is_active else "No",
    } for s in qs[:5000]]
    return {
        "type": "suppliers",
        "columns": _cols(
            ("code", "Code"), ("name", "Name"), ("categories", "Services"),
            ("contact_person", "Contact"), ("contact_phone", "Phone"), ("active", "Active"),
        ),
        "rows": rows, "count": len(rows), "summary": {"total": qs.count()},
    }


def clients_report(request, date_from, date_to):
    from apps.clients.models import Client

    qs = _date_filter(Client.objects.all(), date_from, date_to)
    rows = [{
        "code": c.code, "name": c.name, "contact_person": c.contact_person,
        "contact_phone": c.contact_phone, "contact_email": c.contact_email,
        "active": "Yes" if c.is_active else "No",
    } for c in qs[:5000]]
    return {
        "type": "clients",
        "columns": _cols(
            ("code", "Code"), ("name", "Name"), ("contact_person", "Contact"),
            ("contact_phone", "Phone"), ("contact_email", "Email"), ("active", "Active"),
        ),
        "rows": rows, "count": len(rows), "summary": {"total": qs.count()},
    }


def teams_report(request, date_from, date_to):
    from apps.teams.models import Project

    qs = _date_filter(Project.objects.select_related("client", "site", "manager"), date_from, date_to)
    status = request.query_params.get("status")
    if status:
        qs = qs.filter(status=status)
    rows = [{
        "name": p.name,
        "status": p.get_status_display(),
        "progress": f"{p.progress}%",
        "client": p.client.name if p.client_id else "",
        "site": p.site.name if p.site_id else "",
        "manager": p.manager.get_full_name() if p.manager_id else "",
        "target_date": p.target_date,
    } for p in qs[:5000]]
    return {
        "type": "teams",
        "columns": _cols(
            ("name", "Project"), ("status", "Status"), ("progress", "Progress"),
            ("client", "Client"), ("site", "Site"), ("manager", "Manager"),
            ("target_date", "Target"),
        ),
        "rows": rows, "count": len(rows),
        "summary": {"total": qs.count(), "by_status": dict(qs.values_list("status").annotate(c=Count("id")))},
    }


BUILDERS = {
    "assets": assets_report,
    "tickets": tickets_report,
    "work_orders": work_orders_report,
    "inventory": inventory_report,
    "suppliers": suppliers_report,
    "clients": clients_report,
    "teams": teams_report,
}
