"""What a purchase-order line is, in words a supplier and a buyer both read.

A line points at things — an asset being bought complete, a component on an
asset's build, a stock row — and the description printed on the order is
worked out from those, so it says the name, the kind and what it is for,
not just a code.
"""
from __future__ import annotations

import re


def _dims(device) -> str:
    if device.diagonal_inches:
        return f'{float(device.diagonal_inches):g}" diagonal'
    parts = [device.length_in, device.width_in, device.depth_in]
    if any(parts):
        return " × ".join(f"{float(p):g}" for p in parts if p) + " in"
    return ""


def describe_asset(device) -> tuple[str, str]:
    """(title, detail) for a complete asset bought from a vendor."""
    kind = device.asset_type.name if device.asset_type_id else ""
    model = str(device.device_model) if device.device_model_id else ""
    # The name people use for it, else its make and model, else its kind.
    title = device.display_name or model or kind or device.asset_code
    bits = []
    if kind and title != kind:
        bits.append(kind)
    if model and title != model:
        bits.append(model)
    dims = _dims(device)
    if dims:
        bits.append(dims)
    if device.serial_number:
        bits.append(f"S/N {device.serial_number}")
    bits.append(f"{device.asset_code} · complete asset")
    return title, " · ".join(bits)


def describe_component(component) -> tuple[str, str]:
    """(title, detail) for a part on an asset's build."""
    category = ""
    unit = component.unit or ""
    sku = ""
    if component.inventory_item_id:
        mt = component.inventory_item.material_type
        category = mt.category.name if mt is not None and mt.category_id else ""
        unit = unit or (mt.unit if mt is not None else "")
        sku = component.inventory_item.sku
    elif component.inventory_unit_type_id:
        ut = component.inventory_unit_type
        category = ut.category.name if ut.category_id else ""
        unit = unit or ut.unit
        sku = ut.type_code
    bits = [b for b in (category, unit, sku) if b]
    device = component.device
    for_asset = f"for {device.asset_code}" + (f" {device.display_name}" if device.display_name else "")
    bits.append(for_asset)
    return component.name, " · ".join(bits)


def line_text(title: str, detail: str) -> str:
    """The stored description: one line the list views can show as is."""
    return f"{title} ({detail})" if detail else title


def describe_item(item) -> tuple[str, str]:
    """(title, detail) for an existing PO line, from what it points at; falls
    back to the stored text so older orders still read sensibly."""
    # A component on an asset's build that this line was raised to cover.
    components = list(item.asset_components.all()) if hasattr(item, "asset_components") else []
    if components:
        title, detail = describe_component(components[0])
        if len(components) > 1:
            detail += f" (+{len(components) - 1} more)"
        return title, detail
    devices = list(item.procured_devices.all()) if hasattr(item, "procured_devices") else []
    if devices:
        title, detail = describe_asset(devices[0])
        if len(devices) > 1:
            detail += f" (+{len(devices) - 1} more)"
        return title, detail
    # Text stored as "Title (detail)" by raise-po: the title stands alone.
    m = re.match(r"^(.*?) \((.*)\)$", item.description or "")
    if m:
        return m.group(1), m.group(2)
    if item.bom_line_id:
        # A project BOM line (the older planning model): what it names, for which project.
        bom = item.bom_line
        bits = []
        if bom.material_type_id:
            mt = bom.material_type
            if mt.category_id:
                bits.append(mt.category.name)
            bits.append(mt.name)
        elif bom.device_model_id:
            bits.append(str(bom.device_model))
        elif bom.asset_type_id:
            bits.append(bom.asset_type.name)
        if bom.project_id:
            bits.append(f"for {bom.project.name}")
        return (item.description or bom.description or "—"), " · ".join(bits)
    detail = []
    if item.inventory_unit_type_id:
        ut = item.inventory_unit_type
        if ut.category_id:
            detail.append(ut.category.name)
        detail.append(ut.type_code)
    elif item.inventory_item_id:
        inv = item.inventory_item
        mt = inv.material_type
        if mt is not None and mt.category_id:
            detail.append(mt.category.name)
        detail.append(inv.sku)
    elif item.material_type_id:
        mt = item.material_type
        if mt.category_id:
            detail.append(mt.category.name)
    elif item.asset_type_id:
        detail.append(item.asset_type.name)
    return (item.description or "—"), " · ".join(detail)
