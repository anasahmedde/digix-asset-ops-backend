"""One project, start to finish, as an Excel workbook.

Summary · Cost (planned against actual, by asset and category) · Components ·
Production · Assets & installation (where each went, the crew and every date)
· Site visits (check-ins) · Overheads · Milestones · Activity. The cost sheets
are left out for anyone without the right to see project costs.
"""

from __future__ import annotations

from decimal import Decimal

from .xlsx import ReportBook
from .builders import _name, col


def _d(v):
    return None if v is None else Decimal(v)


def _var(planned, actual):
    """Over plan is positive. Nothing recorded yet is no variance, not a saving."""
    if actual is None:
        return None
    return actual - (planned or 0)


def build_project_workbook(project, user, show_costs: bool) -> ReportBook:
    from apps.sites.activity import presence_rows
    from apps.sites.models import DeviceInstallation
    from apps.sites.serializers import _health
    from apps.teams.costing import build_actuals, build_plan, project_devices
    from apps.teams.models import Project

    devices = list(project_devices(project).select_related("asset_type", "device_model", "current_site"))
    installs = list(
        DeviceInstallation.objects.filter(device__in=devices)
        .select_related("device", "site", "zone", "installed_by", "vendor", "handover")
        .prefetch_related("helpers").order_by("device__asset_code", "installed_at")
    )
    progress = project.computed_progress()
    health = dict(Project.Status.choices).get(project.health(progress), project.get_status_display())
    sites = ", ".join(s.name for s in project.sites.all()) or (project.site.name if project.site_id else "")
    heading = f"{project.project_code} · {project.name}" if project.project_code else project.name
    book = ReportBook(f"Project report — {heading}", user)
    sub = [f"Client: {project.client.name}" if project.client_id else "", f"Sites: {sites}" if sites else ""]

    handed = {i.device_id for i in installs if getattr(i, "handover", None) is not None}
    sections = [
        ("Project", [
            ("Project ID", project.project_code, "text"), ("Project", project.name, "text"),
            ("Client", project.client.name if project.client_id else "", "text"), ("Sites", sites, "text"),
            ("Project manager", _name(project.manager), "text"),
            ("Contract", project.get_contract_type_display() if project.contract_type else "", "text"),
            ("Phase", project.get_phase_display(), "text"), ("Health", health, "text"),
            ("Progress", progress, "percent"), ("Description", project.description, "text"),
        ]),
        ("Dates", [
            ("Created", project.created_at, "date"), ("Start date", project.start_date, "date"),
            ("Closing date", project.target_date, "date"), ("Completed", project.completed_date, "date"),
            ("Rental ends", project.rental_end_date, "date"),
        ]),
        ("Delivery", [
            ("Assets in scope", len(devices), "int"),
            ("Installed", sum(1 for d in devices if d.installation_date), "int"),
            ("Live (active)", sum(1 for d in devices if d.status in ("active", "client_property")), "int"),
            ("Handed over to the client", len(handed), "int"),
        ]),
    ]

    plan = actual = None
    if show_costs:
        plan, actual = build_plan(project), build_actuals(project)
        approved = _d(actual["approved_total"])
        spent = _d(actual["actual_total"])
        sections.insert(2, ("Budget", [
            ("Budget status", plan["status_display"] if plan["has_plan"] else "No budget yet", "text"),
            ("Approved budget", approved, "money"), ("Estimate", _d(plan["total"]), "money"),
            ("Actual cost to date", spent, "money"),
            ("Variance against approved", None if approved is None else spent - approved, "variance"),
            ("Budget used", None if not approved else float(spent / approved * 100), "percent"),
            ("Approved by", plan["decided_by"] or "", "text"), ("Approved on", plan["decided_at"], "date"),
        ]))
    book.summary("Summary", f"Project report — {heading}", sections, sub)

    if show_costs:
        planned_assets = {a["id"]: a for a in plan["assets"]}
        cost_rows = []
        for a in actual["assets"]:
            p = planned_assets.get(a["id"], {})
            label = f'{a["asset_code"]} {a["asset_name"]}'.strip()
            pairs = (
                ("Asset (bought complete)" if a["vendor_asset"] else "Components",
                 p.get("materials_total"), a["materials_actual"]),
                ("Production", p.get("production_total"),
                 _d(a["production_actual"]) + _d(a["work_orders_actual"])),
                ("Installation & activation", p.get("installation_cost"), a["installation_actual"]),
            )
            for category, planned, spent in pairs:
                # Nothing spent yet is "not recorded", not a saving against plan.
                planned, spent = _d(planned), _d(spent) or None
                if not planned and spent is None:
                    continue
                cost_rows.append({"asset": label, "category": category, "planned": planned, "actual": spent,
                                  "variance": _var(planned, spent)})
        spent = _d(actual["overheads_actual_total"]) or None
        cost_rows.append({"asset": "Project", "category": "Overheads", "planned": _d(plan["overheads_total"]),
                          "actual": spent, "variance": _var(_d(plan["overheads_total"]), spent)})
        if _d(plan["contingency_amount"]):
            cost_rows.append({"asset": "Project", "category": f'Contingency ({plan["contingency_percent"]}% on materials)',
                              "planned": _d(plan["contingency_amount"]), "actual": None, "variance": None})
        book.table("Cost", "Cost — planned against actual", [
            col("asset", "Asset"), col("category", "Category"), col("planned", "Planned", "money"),
            col("actual", "Actual", "money"), col("variance", "Variance (over +)", "variance"),
        ], cost_rows, [
            f"Approved budget: PKR {_d(actual['approved_total']):,.2f}" if actual["approved_total"] is not None
            else "Budget not approved yet",
            "Variance is shown once a cost is recorded: over plan in red, under in green.",
        ])

        planned_lines = {m["component"]: m for m in plan["materials"]}
        comp_rows = []
        for a in actual["assets"]:
            for line in a["lines"]:
                p = planned_lines.get(line["component"], {})
                comp_rows.append({
                    "asset": a["asset_code"], "component": line["name"], "unit": line["unit"],
                    "required": line["required"], "used": line["issued"],
                    "planned_price": p.get("unit_price"), "planned": p.get("line_total"), "priced_from": p.get("price_source", ""),
                    "actual_price": line["unit_price"], "actual": line["line_total"], "valued_at": line["price_source"],
                    "variance": _var(_d(p.get("line_total")), _d(line["line_total"])),
                })
        book.table("Components", "Components — planned against actual", [
            col("asset", "Asset"), col("component", "Component"), col("unit", "Unit"),
            col("required", "Required", "qty", False), col("used", "Used", "qty", False),
            col("planned_price", "Planned unit price", "money", False), col("planned", "Planned", "money"),
            col("priced_from", "Priced from"), col("actual_price", "Actual unit price", "money", False),
            col("actual", "Actual", "money"), col("valued_at", "Valued at"), col("variance", "Variance", "variance"),
        ], comp_rows)

        step_rows = []
        for a in actual["assets"]:
            for st in a["steps"]:
                where = ("Vendor" if st["work_order"] or st["location"] == "external"
                         else "In-house" if st["location"] == "in_house" else "Not decided")
                step_rows.append({
                    "asset": a["asset_code"], "step": st["step_number"], "operation": st["name"], "where": where,
                    "vendor": st["work_order"]["supplier"] if st["work_order"] else "",
                    "status": (st.get("status") or "").replace("_", " ").capitalize(),
                    "planned": st["planned_cost"], "actual": st["actual_cost"], "source": st["actual_source"],
                    "variance": _var(_d(st["planned_cost"]), _d(st["actual_cost"])),
                })
            for w in a["work_orders"]:
                step_rows.append({"asset": a["asset_code"], "operation": f'Work order {w["wo_number"]}', "where": "Vendor",
                                  "vendor": w["supplier"], "status": w["status"].replace("_", " ").capitalize(),
                                  "actual": w["amount"], "source": "Work order"})
        book.table("Production", "Production — each operation, planned against actual", [
            col("asset", "Asset"), col("step", "Step", "int"), col("operation", "Operation"), col("where", "Where"),
            col("vendor", "Vendor"), col("status", "Status"), col("planned", "Planned", "money"),
            col("actual", "Actual", "money"), col("source", "Actual from"), col("variance", "Variance", "variance"),
        ], step_rows)

    by_device = {}
    for inst in installs:
        by_device.setdefault(inst.device_id, []).append(inst)
    asset_rows, visit_rows = [], []
    for d in devices:
        jobs = by_device.get(d.pk) or [None]
        for inst in jobs:
            record = getattr(inst, "handover", None) if inst else None
            arrived = []
            if inst is not None:
                for pr in presence_rows(inst):
                    if pr["action"] == "checked_in":
                        arrived.append(pr["_at"])
                    visit_rows.append({
                        "asset": d.asset_code, "site": inst.site.name, "who": pr["actor"] or "",
                        "what": "Checked in" if pr["action"] == "checked_in" else "Checked out",
                        "at": pr["_at"], "detail": pr["text"],
                    })
            row = {
                "asset": d.asset_code, "name": d.display_name, "type": d.asset_type.name if d.asset_type_id else "",
                "model": str(d.device_model) if d.device_model_id else "", "serial": d.serial_number,
                "source": d.get_source_display(), "status": d.get_status_display(),
                "site": inst.site.name if inst else (d.current_site.name if d.current_site_id else ""),
                "city": inst.site.city if inst else (d.current_site.city if d.current_site_id else ""),
                "position": (inst.position_label or (inst.zone.name if inst.zone_id else "")) if inst else "",
                "installer": (_name(inst.installed_by) or (inst.vendor.name if inst.vendor_id else inst.external_vendor_name)) if inst else "",
                "helpers": ", ".join(_name(h) for h in inst.helpers.all()) if inst else "",
                "booked": inst.installed_at if inst else None, "due": inst.due_date if inst else None,
                "first_check_in": min(arrived) if arrived else None,
                "completed": inst.completed_at if inst else None,
                "installed_on": d.installation_date,
                "handover": record.handover_date if record else None,
                "accepted_by": record.accepted_by_name if record else "",
                "job": ("Removed" if inst.removed_at else _health(inst)[1]) if inst else "Not booked yet",
            }
            if show_costs:
                row.update({"install_planned": d.planned_installation_cost, "install_actual": d.actual_installation_cost})
            asset_rows.append(row)
    asset_cols = [
        col("asset", "Asset ID"), col("name", "Asset name"), col("type", "Type"), col("model", "Model"),
        col("serial", "Serial no."), col("source", "Source"), col("status", "Asset status"),
        col("site", "Installed at (site)"), col("city", "City"), col("position", "Position at site"),
        col("installer", "Installer"), col("helpers", "Helpers"), col("booked", "Booked on", "date"),
        col("due", "Due", "date"), col("first_check_in", "First check-in", "datetime"),
        col("completed", "Installation completed", "datetime"), col("installed_on", "Installed on", "date"),
        col("handover", "Handed over", "date"), col("accepted_by", "Accepted by"), col("job", "Installation status"),
    ]
    if show_costs:
        asset_cols += [col("install_planned", "Installation planned", "money"), col("install_actual", "Installation actual", "money")]
    book.table("Assets & installation", "Assets — where each was installed, and when", asset_cols, asset_rows,
               totals=show_costs)
    visit_rows.sort(key=lambda r: (r["asset"], r["at"]))
    book.table("Site visits", "Site visits — crew check-ins and check-outs", [
        col("asset", "Asset"), col("site", "Site"), col("who", "Person"), col("what", "Event"),
        col("at", "When", "datetime"), col("detail", "Detail"),
    ], visit_rows, totals=False, empty="No check-ins recorded for this project's installations.")

    if show_costs:
        book.table("Overheads", "Overheads — planned against actual", [
            col("type", "Cost type"), col("description", "Description"), col("planned", "Planned", "money"),
            col("qty", "Actual qty", "qty", False), col("rate", "Actual rate", "money", False),
            col("actual", "Actual", "money"), col("variance", "Variance", "variance"), col("unplanned", "Unplanned"),
        ], [{
            "type": o["cost_type"], "description": o["description"], "planned": o["planned_amount"],
            "qty": o["actual_quantity"], "rate": o["actual_unit_cost"], "actual": o["actual_amount"],
            "variance": _var(_d(o["planned_amount"]), _d(o["actual_amount"])),
            "unplanned": "Yes" if o["unplanned"] else "",
        } for o in actual["overheads"]])

    book.table("Milestones", "Milestones", [
        col("title", "Milestone"), col("due", "Due", "date"), col("done", "Completed", "datetime"), col("state", "State"),
    ], [{
        "title": m.title, "due": m.due_date, "done": m.completed_at,
        "state": ("Done late" if m.due_date and m.completed_at and m.completed_at.date() > m.due_date
                  else "Done" if m.completed_at else "Open"),
    } for m in project.milestones.all()], totals=False, empty="No milestones set.")

    book.table("Activity", "Activity log", [
        col("at", "When", "datetime"), col("who", "Who"), col("what", "What happened"),
    ], [{"at": a.at, "who": _name(a.actor) if a.actor_id else "System", "what": a.text}
        for a in project.activity.select_related("actor").order_by("at")[:2000]], totals=False)
    return book
