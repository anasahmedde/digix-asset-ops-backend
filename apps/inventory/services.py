"""Stock-side effects shared by every issuance path (direct site issuance,
BOM-line issue, and fitting components into an asset). Callers must wrap in
transaction.atomic()."""

from rest_framework import serializers

from .models import GoodsReceiptLine, InventoryItem, InventoryUnit, StockMovement


class _LazyFulfilment:
    """AssetComponent.Fulfilment without importing assets at module load."""

    def __getattr__(self, name):
        from apps.assets.models import AssetComponent

        return getattr(AssetComponent.Fulfilment, name)


AssetComponentFulfilment = _LazyFulfilment()


def apply_issuance_stock_out(issuance, user):
    """Atomic decrement of on-hand stock + OUT movement for a saved Issuance.

    Locks the inventory row so concurrent issues can't double-spend.
    """
    item = InventoryItem.objects.select_for_update().get(pk=issuance.item_id)
    item.quantity -= issuance.quantity
    item.save(update_fields=["quantity", "updated_at"])
    StockMovement.objects.create(
        item=item,
        movement_type=StockMovement.MovementType.OUT,
        quantity=issuance.quantity,
        reference=issuance.issue_number,
        performed_by=user,
        notes=issuance.reason or f"Issued {issuance.issue_number}",
    )
    return item


def issue_stock_for_component(component, user, quantity):
    """Draw ``quantity`` out of the warehouse to cover an asset requirement.

    This is the only place a component moves stock, and it runs when the
    project decides to cover the requirement from inventory — never when the
    requirement itself is written down. Caller must wrap in transaction.atomic().
    """
    device_code = component.device.asset_code

    if quantity < 1:
        raise serializers.ValidationError({"quantity": "Issue at least one."})
    if quantity > component.outstanding_quantity:
        raise serializers.ValidationError({
            "quantity": (
                f"Only {component.outstanding_quantity} of this requirement is still "
                f"outstanding ({component.issued_quantity} of {component.quantity} already issued)."
            )
        })

    issued_units = []

    if component.inventory_item_id:
        item = InventoryItem.objects.select_for_update().get(pk=component.inventory_item_id)
        if item.quantity < quantity:
            raise serializers.ValidationError({
                "quantity": (
                    f"Only {item.quantity} of {item.material_type.name} in stock — "
                    f"procure the shortfall instead."
                )
            })
        item.quantity -= quantity
        item.save(update_fields=["quantity", "updated_at"])
        StockMovement.objects.create(
            item=item,
            movement_type=StockMovement.MovementType.OUT,
            quantity=quantity,
            reference=device_code,
            performed_by=user,
            notes=f"Issued to asset {device_code} for '{component.name}'",
        )
    elif component.inventory_unit_type_id:
        # Take that many in-stock serials of the opened product, oldest first.
        units = list(
            InventoryUnit.objects.select_for_update()
            .filter(unit_type_id=component.inventory_unit_type_id,
                    status=InventoryUnit.Status.IN_STOCK)
            .order_by("created_at")[:quantity]
        )
        if len(units) < quantity:
            raise serializers.ValidationError({
                "quantity": (
                    f"Only {len(units)} unit(s) of {component.inventory_unit_type} in stock — "
                    f"procure the shortfall instead."
                )
            })
        for unit in units:
            unit.status = InventoryUnit.Status.ISSUED
            unit.save(update_fields=["status", "updated_at"])
            issued_units.append(unit)
        # Record the first serial on the requirement when it covers one unit.
        if component.inventory_unit_id is None and len(units) == 1:
            component.inventory_unit = units[0]
    else:
        raise serializers.ValidationError(
            {"detail": "This requirement has no inventory item to draw from."}
        )

    component.issued_quantity += quantity
    component.fulfilment = (
        AssetComponentFulfilment.FULFILLED if component.outstanding_quantity == 0
        else AssetComponentFulfilment.FROM_STOCK
    )
    component.save(update_fields=["issued_quantity", "fulfilment", "inventory_unit", "updated_at"])

    record_build_progress(component, user, f"Issued {quantity} × {component.name} from inventory")
    return {"units": issued_units, "quantity": quantity}


def record_build_progress(component, user, description):
    """Show the build on the asset's own lifecycle, and start it when parts move.

    The project screen drives the work, but the asset is where people look for
    its history — so every fulfilment step is journalled there. Drawing the
    first parts also moves a procured asset onto the production floor, since
    that is physically what has happened.
    """
    from apps.assets.models import Device, DeviceLifecycleEvent

    device = component.device
    DeviceLifecycleEvent.objects.create(
        device=device,
        event_type=DeviceLifecycleEvent.EventType.NOTE,
        description=description,
        performed_by=user,
        metadata={
            "component": str(component.pk),
            "issued": component.issued_quantity,
            "required": component.quantity,
        },
    )

    # The floor starts once the whole parts list is in hand, not on the first
    # item issued — the same rule the registry enforces on the manual move.
    if device.status == Device.Status.PROCURED and not any(
        c.outstanding_quantity > 0 for c in device.components.all()
    ):
        device.status = Device.Status.IN_PRODUCTION
        device._transition_user = user
        device._transition_reason = "Build started — every component fulfilled"
        device.save(update_fields=["status", "updated_at"])


def return_stock_for_component(component, user):
    """Put back everything issued against a requirement (undo)."""
    if component.issued_quantity <= 0:
        return

    device_code = component.device.asset_code
    if component.inventory_item_id:
        item = InventoryItem.objects.select_for_update().get(pk=component.inventory_item_id)
        item.quantity += component.issued_quantity
        item.save(update_fields=["quantity", "updated_at"])
        StockMovement.objects.create(
            item=item,
            movement_type=StockMovement.MovementType.IN,
            quantity=component.issued_quantity,
            reference=device_code,
            performed_by=user,
            notes=f"Returned from asset {device_code} ('{component.name}' requirement cleared)",
        )
    elif component.inventory_unit_type_id:
        units = InventoryUnit.objects.select_for_update().filter(
            unit_type_id=component.inventory_unit_type_id,
            status=InventoryUnit.Status.ISSUED,
        ).order_by("-updated_at")[:component.issued_quantity]
        for unit in units:
            unit.status = InventoryUnit.Status.IN_STOCK
            unit.save(update_fields=["status", "updated_at"])

    component.issued_quantity = 0
    component.inventory_unit = None
    component.fulfilment = AssetComponentFulfilment.PENDING
    component.save(update_fields=["issued_quantity", "inventory_unit", "fulfilment", "updated_at"])


def stock_inspected_line(line, *, user, route, accepted_quantity, rejected_quantity=0,
                         notes="", generic=None, units=None):
    """Route an inspected goods-receipt line into the warehouse.

    Nothing reaches inventory until this runs — receipt only queues the line.
    ``route`` decides where the accepted quantity lands:

    * ``generic`` — tops up (or opens) an ``InventoryItem`` for the material
      and journals an IN movement carrying the batch number.
    * ``unique``  — creates one ``InventoryUnit`` per supplied serial.

    Every item created or topped up carries the line's batch number and a link
    back to the receipt line, so stock traces to its GRN and purchase order.
    Caller must wrap in ``transaction.atomic()``.
    """
    from django.utils import timezone

    po_item = line.po_item
    receipt = line.receipt
    batch = line.batch_number
    grn = receipt.grn_number
    po_number = receipt.purchase_order.po_number if receipt.purchase_order_id else ""
    trace = f"GRN {grn}" + (f" · PO {po_number}" if po_number else "")

    created_units = []
    inventory_item = None

    if accepted_quantity:
        if route == GoodsReceiptLine.Route.GENERIC:
            generic = generic or {}
            material_type_id = (
                generic.get("material_type")
                or (po_item.material_type_id if po_item else None)
                # a return names the stock row it came from
                or (line.inventory_item.material_type_id if line.inventory_item_id else None)
            )
            if not material_type_id:
                raise serializers.ValidationError(
                    {"generic": "This line has no material type — pick one to stock it as generic."}
                )
            # Prefer the row the purchase was raised against; only fall back to
            # matching on material when the line does not name one, otherwise
            # goods land on a different row than the requirement is watching.
            named_item_id = (
                (generic or {}).get("inventory_item")
                or (po_item.inventory_item_id if po_item else None)
                or line.inventory_item_id
            )
            inventory_item = (
                InventoryItem.objects.select_for_update().filter(pk=named_item_id).first()
                if named_item_id else None
            )
            if inventory_item is None:
                inventory_item = (
                    InventoryItem.objects.select_for_update()
                    .filter(material_type_id=material_type_id)
                    .order_by("created_at")
                    .first()
                )
            if inventory_item is None:
                inventory_item = InventoryItem.objects.create(
                    material_type_id=material_type_id,
                    category_id=generic.get("category"),
                    quantity=0,
                    min_stock_level=generic.get("min_stock_level", 5),
                    unit_cost=generic.get("unit_cost") or (po_item.unit_price if po_item else None),
                    storage_location=(generic.get("storage_location") or "").strip(),
                )
            elif (generic.get("storage_location") or "").strip():
                # Stock put somewhere new on this delivery: record where.
                inventory_item.storage_location = generic["storage_location"].strip()
            inventory_item.quantity += accepted_quantity
            inventory_item.save(update_fields=["quantity", "updated_at"])
            StockMovement.objects.create(
                item=inventory_item,
                movement_type=StockMovement.MovementType.IN,
                quantity=accepted_quantity,
                reference=grn,
                batch_number=batch,
                goods_receipt_line=line,
                performed_by=user,
                notes=f"Inspection passed — stocked from {trace}",
            )
            line.inventory_item = inventory_item
        else:
            units = units or []
            if len(units) != accepted_quantity:
                raise serializers.ValidationError({
                    "units": f"Provide details for exactly {accepted_quantity} unit(s); got {len(units)}."
                })
            serials = [str(u.get("serial_number", "")).strip() for u in units]
            if any(not s for s in serials):
                raise serializers.ValidationError({"units": "Every unit needs a serial number."})
            if len(set(serials)) != len(serials):
                raise serializers.ValidationError({"units": "Serial numbers must be unique."})
            clashes = list(
                InventoryUnit.objects.filter(serial_number__in=serials)
                .values_list("serial_number", flat=True)[:5]
            )
            if clashes:
                raise serializers.ValidationError(
                    {"units": "Already in inventory: " + ", ".join(clashes)}
                )

            material_type_id = po_item.material_type_id if po_item else None
            # Serialized PO lines describe a device model, not a material —
            # carry that identity onto the unit so the technician need not
            # retype what the order already says.
            po_model = po_item.device_model if po_item and po_item.device_model_id else None
            # The PO line may already name the opened product; otherwise the
            # inspector can supply it once for the whole line.
            stashed = (line.inspection_notes or "")
            line_unit_type_id = (
                (po_item.inventory_unit_type_id if po_item else None)
                or (generic or {}).get("unit_type")
                # a return of a unique component names its product on the line
                or (stashed.split("unit_type:", 1)[1].split()[0] if "unit_type:" in stashed else None)
            )
            for payload in units:
                material = payload.get("material_type") or material_type_id
                brand = payload.get("brand") or (po_model.brand_id if po_model else None)
                model_name = payload.get("model_name") or (po_model.name if po_model else "")
                # A pre-opened product supplies everything but the serial.
                unit_type_id = payload.get("unit_type") or line_unit_type_id
                if not unit_type_id and not material and not model_name:
                    raise serializers.ValidationError(
                        {"units": "Each unit needs an inventory product, a material type or a model name."}
                    )
                created_units.append(InventoryUnit.objects.create(
                    serial_number=str(payload["serial_number"]).strip(),
                    unit_type_id=unit_type_id,
                    material_type_id=material,
                    category_id=payload.get("category"),
                    brand_id=brand,
                    model_name=model_name,
                    supplier_id=(
                        payload.get("supplier")
                        or (receipt.purchase_order.supplier_id if receipt.purchase_order_id else None)
                    ),
                    purchase_price=payload.get("purchase_price") or (po_item.unit_price if po_item else None),
                    purchase_date=payload.get("purchase_date") or timezone.localdate(),
                    # Batch + receipt link: this is the trace back to the PO.
                    batch_number=batch,
                    goods_receipt_line=line,
                    # Item 23: the storekeeper types the term; the start is the
                    # day the part arrived and the vendor gave the cover.
                    has_warranty=bool(payload.get("warranty_months")) or bool(payload.get("has_warranty")),
                    warranty_type=(
                        payload.get("warranty_type") or "supplier"
                        if (payload.get("warranty_months") or payload.get("has_warranty")) else ""
                    ),
                    warranty_start=(
                        (payload.get("warranty_start") or timezone.localdate())
                        if (payload.get("warranty_months") or payload.get("has_warranty")) else None
                    ),
                    warranty_months=payload.get("warranty_months"),
                    warranty_end=payload.get("warranty_end"),
                    notes=f"Received on {trace}",
                ))

    # Fulfil the project BOM line the purchase was raised against, now that
    # the goods have actually passed inspection and entered the warehouse.
    bom_line = po_item.bom_line if po_item else None
    if bom_line and accepted_quantity:
        from apps.teams.models import BOMAllocation

        if created_units:
            for unit in created_units:
                BOMAllocation.objects.create(
                    bom_line=bom_line, inventory_unit=unit, quantity=1,
                    status=BOMAllocation.Status.ALLOCATED, allocated_by=user,
                )
        elif inventory_item is not None:
            BOMAllocation.objects.create(
                bom_line=bom_line, inventory_item=inventory_item, quantity=accepted_quantity,
                status=BOMAllocation.Status.ALLOCATED, allocated_by=user,
            )

    # Goods bought *for* a specific requirement belong to it. Issue them
    # straight through so the project shows the line covered and the asset is
    # ready to build, rather than leaving the stock to be claimed by anything
    # else that happens to need the same material.
    if accepted_quantity and po_item is not None:
        remaining = accepted_quantity
        for component in po_item.asset_components.select_related("device").all():
            if remaining <= 0:
                break
            take = min(component.outstanding_quantity, remaining)
            if take > 0:
                issue_stock_for_component(component, user, take)
                remaining -= take

    line.inspection_status = (
        GoodsReceiptLine.Inspection.PASSED if accepted_quantity
        else GoodsReceiptLine.Inspection.REJECTED
    )
    line.routed_to = route if accepted_quantity else ""
    line.accepted_quantity = accepted_quantity
    line.rejected_quantity = rejected_quantity
    line.inspected_by = user
    line.inspected_at = timezone.now()
    line.inspection_notes = notes
    line.save(update_fields=[
        "inspection_status", "routed_to", "accepted_quantity", "rejected_quantity",
        "inspected_by", "inspected_at", "inspection_notes", "inventory_item", "updated_at",
    ])
    return {"inventory_item": inventory_item, "units": created_units}


def issue_against_request(request_row, user, quantity, *, received_by="", notes=""):
    """Hand over ``quantity`` against a material request.

    The store is the only place stock leaves from, so this is where the
    warehouse actually moves. A request that cannot be met in full is issued
    in part and the balance stays on the queue.

    Caller must wrap in ``transaction.atomic()``.
    """
    from .models import IssuanceRequest

    if request_row.status == IssuanceRequest.Status.CANCELLED:
        raise serializers.ValidationError({"detail": "This request was cancelled."})
    if quantity < 1:
        raise serializers.ValidationError({"quantity": "Issue at least one."})
    if quantity > request_row.outstanding_quantity:
        raise serializers.ValidationError({
            "quantity": (
                f"Only {request_row.outstanding_quantity} still outstanding on this request "
                f"({request_row.quantity_issued} of {request_row.quantity_requested} already issued)."
            )
        })

    serials = []

    # A request raised against a build moves that requirement along, so it goes
    # through the same path the requirement has always used.
    if request_row.asset_component_id:
        result = issue_stock_for_component(request_row.asset_component, user, quantity)
        serials = [u.serial_number for u in result["units"]]
    elif request_row.item_id:
        item = InventoryItem.objects.select_for_update().get(pk=request_row.item_id)
        if item.quantity < quantity:
            raise serializers.ValidationError({
                "quantity": f"Only {item.quantity} in stock — issue what you have, or wait for a delivery."
            })
        item.quantity -= quantity
        item.save(update_fields=["quantity", "updated_at"])
        StockMovement.objects.create(
            item=item,
            movement_type=StockMovement.MovementType.OUT,
            quantity=quantity,
            reference=request_row.request_number,
            performed_by=user,
            notes=notes or f"Issued against {request_row.request_number}",
        )
    elif request_row.unit_type_id:
        units = list(
            InventoryUnit.objects.select_for_update()
            .filter(unit_type_id=request_row.unit_type_id, status=InventoryUnit.Status.IN_STOCK)
            .order_by("created_at")[:quantity]
        )
        if len(units) < quantity:
            raise serializers.ValidationError({
                "quantity": f"Only {len(units)} unit(s) in stock — issue what you have, or wait for a delivery."
            })
        for unit in units:
            unit.status = InventoryUnit.Status.ISSUED
            unit.save(update_fields=["status", "updated_at"])
            serials.append(unit.serial_number)
    else:
        raise serializers.ValidationError(
            {"detail": "This request does not name anything to issue."}
        )

    request_row.quantity_issued += quantity
    request_row.issued_by = user
    if received_by:
        request_row.received_by = received_by
    if serials:
        request_row.issued_serials = [*(request_row.issued_serials or []), *serials]
    request_row.sync_status()
    request_row.save(update_fields=[
        "quantity_issued", "issued_by", "received_by", "issued_serials", "status", "updated_at",
    ])
    return {"quantity": quantity, "serials": serials}
