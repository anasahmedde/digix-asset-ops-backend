"""What an asset has cost, from the two places that spend on it."""
from decimal import Decimal


def money(value) -> Decimal:
    return Decimal(value or 0).quantize(Decimal("0.01"))


def build_or_buy_cost(device):
    """What the project spent getting this asset in place.

    Read from the project's own costing rather than worked out again here:
    materials, production, work orders and installation are already added
    up there, asset by asset, and two answers to one question is how they
    come to disagree.
    """
    project = device.project_on
    if project is None:
        return None
    from apps.teams.costing import build_actuals

    try:
        actuals = build_actuals(project)
    except Exception:
        # A half-set-up project should not take the asset's page down with
        # it; the page says the figure is unavailable instead.
        return None
    row = next(
        (a for a in actuals.get("assets", []) if str(a.get("id")) == str(device.pk)),
        None,
    )
    if row is None:
        return None
    # An asset bought complete has no build to account for: one price, from
    # the order that bought it. Showing it as "materials" was true of the
    # arithmetic and wrong about the asset.
    vendor = bool(row.get("vendor_asset"))
    return {
        "project": str(project.pk),
        "project_name": project.name,
        "vendor_asset": vendor,
        "purchase_price": row.get("asset_price") if vendor else None,
        "priced_from": row.get("asset_priced_from") if vendor else None,
        "materials": None if vendor else row.get("materials_actual"),
        # Making it costs what the floor spent on it and what was paid to
        # anybody outside on a work order. They are one cost — splitting
        # them made a reader add two numbers to answer one question.
        "production": None if vendor else money(
            money(row.get("production_actual") or 0) + money(row.get("work_orders_actual") or 0)
        ),
        # What it actually cost to put up, as recorded on the project's
        # execution side. A planned figure is a forecast, and a forecast
        # has no business being added into what an asset has cost.
        "installation": row.get("installation_actual"),
        # What it cost to obtain, with installation left out: the two are
        # separate headings now, and a figure that appears under both is
        # a figure somebody will count twice.
        "obtained": str(
            money(row.get("actual_total") or 0) - money(row.get("installation_actual") or 0)
        ),
        "total": row.get("actual_total"),
    }


def maintenance_cost(device):
    """What has been spent keeping it running, split by what kind of work.

    Only accepted work counts: a visit is priced when the office accepts
    it, and a visit still being argued over is not a cost yet.
    """
    from apps.maintenance.models import MaintenanceRecord, MaintenanceSchedule

    done = (
        MaintenanceRecord.objects
        .filter(schedule__device=device, status=MaintenanceRecord.Status.COMPLETED)
        .select_related("schedule")
    )
    out = {}
    for kind in (MaintenanceSchedule.MaintenanceType.PREVENTIVE,
                 MaintenanceSchedule.MaintenanceType.CORRECTIVE):
        rows = [r for r in done if r.schedule.maintenance_type == kind]
        out[kind] = {
            "visits": len(rows),
            "total": money(sum((r.cost or 0) for r in rows)),
        }
    out["total"] = money(out["preventive"]["total"] + out["corrective"]["total"])
    return out


def cost_of_ownership(device):
    """Getting it in place, keeping it running, and the two added together."""
    built = build_or_buy_cost(device)
    kept = maintenance_cost(device)
    getting_there = money(built.get("total") or 0) if built else Decimal("0")
    return {"development": built, "maintenance": kept,
            "total": money(kept["total"] + getting_there)}
