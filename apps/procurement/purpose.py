"""Who a purchase order was raised for.

Nothing on an order records this, and nothing needs to: every line already
points at the thing it was bought for — a component on an asset, an asset on a
project, a stock row that ran low. Reading those back gives the answer without
another field to keep in step.
"""
from __future__ import annotations


def describe_purpose(purchase_order) -> dict:
    """{kind, label, detail} — what this order is for, in words.

    ``kind`` is one of "project", "stock", "mixed" or "unknown", for whoever
    wants to colour it; ``label`` is the line to show; ``detail`` names the
    assets, where the order is for assets.
    """
    projects: dict = {}
    assets: list[str] = []
    for_stock = False

    def note(device):
        # An asset reaches a project by its own link or by a Scope row, so ask
        # the asset rather than reading one field and missing the other.
        project = device.project_on
        if project is not None:
            projects[project.pk] = project.name
        if device.asset_code and device.asset_code not in assets:
            assets.append(device.asset_code)

    for item in purchase_order.items.all():
        for component in item.asset_components.all():
            note(component.device)
        for device in item.procured_devices.all():
            note(device)
        if item.bom_line_id and item.bom_line.project_id:
            projects[item.bom_line.project_id] = item.bom_line.project.name
        # A line that tops the warehouse up: raised from a reorder request, or
        # naming a stock row with no project behind it.
        if item.reorder_requests.exists():
            for_stock = True
        elif not item.asset_components.exists() and not item.procured_devices.exists():
            if item.inventory_item_id or item.inventory_unit_type_id or item.material_type_id:
                for_stock = True

    detail = " · ".join(assets[:3]) + (f" +{len(assets) - 3} more" if len(assets) > 3 else "")
    names = sorted(projects.values())

    if names and for_stock:
        label = names[0] if len(names) == 1 else f"{len(names)} projects"
        return {"kind": "mixed", "label": f"{label} & stock", "detail": detail}
    if names:
        if len(names) == 1:
            return {"kind": "project", "label": names[0], "detail": detail}
        return {"kind": "mixed", "label": f"{len(names)} projects", "detail": ", ".join(names)}
    if for_stock:
        return {"kind": "stock", "label": "Inventory restock", "detail": detail}
    return {"kind": "unknown", "label": "Not linked", "detail": detail}
