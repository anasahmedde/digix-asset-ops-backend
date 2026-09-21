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


def _settle_requests_for_component(component, issued, *, exclude=None):
    """Stock that reached a requirement counts against the requests raised for it.

    Goods can reach a requirement without passing through the store's queue —
    bought for it and issued straight from inspection, for instance. The open
    requests for that requirement are settled in the order they were raised,
    and once the requirement is fully covered anything still owed on the queue
    is closed, since there is nothing left to hand over.
    """
    from .models import IssuanceRequest

    open_requests = (
        IssuanceRequest.objects.select_for_update()
        .filter(asset_component=component)
        .exclude(status=IssuanceRequest.Status.CANCELLED)
        .order_by("created_at")
    )
    if exclude is not None:
        open_requests = open_requests.exclude(pk=exclude.pk)
    remaining = issued
    for req in open_requests:
        owed = req.outstanding_quantity
        if owed <= 0:
            continue
        if remaining > 0:
            take = min(owed, remaining)
            req.quantity_issued += take
            req.notes = (req.notes + "\n" if req.notes else "") + (
                f"{take} covered by stock issued straight to the requirement."
            )
            from django.utils import timezone as _tz

            req.last_issued_at = _tz.now()
            req.handovers = [*(req.handovers or []), {
                "at": req.last_issued_at.isoformat(), "quantity": take, "received_by": "",
                "issued_by": "", "serials": [], "note": "Issued straight to the requirement.",
            }]
            req.sync_status()
            req.save(update_fields=["quantity_issued", "notes", "status", "last_issued_at", "handovers", "updated_at"])
            remaining -= take
        elif component.outstanding_quantity == 0:
            req.status = IssuanceRequest.Status.CANCELLED
            req.notes = (req.notes + "\n" if req.notes else "") + (
                "Closed — the requirement was already covered from stock."
            )
            req.save(update_fields=["status", "notes", "updated_at"])


def issue_stock_for_component(component, user, quantity, *, via_request=None):
    """Draw ``quantity`` out of the warehouse to cover an asset requirement.

    This is the only place a component moves stock, and it runs when the
    project decides to cover the requirement from inventory — never when the
    requirement itself is written down. Caller must wrap in transaction.atomic().
    ``via_request`` is the store request being served, if any; other open
    requests for the same requirement are settled by what was issued.
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
    if component.outstanding_quantity == 0:
        component.fulfilment = AssetComponentFulfilment.FULFILLED
    elif component.fulfilment != AssetComponentFulfilment.PROCUREMENT:
        # A line partly bought stays 'to be procured' until the rest arrives.
        component.fulfilment = AssetComponentFulfilment.FROM_STOCK
    component.save(update_fields=["issued_quantity", "fulfilment", "inventory_unit", "updated_at"])

    record_build_progress(component, user, f"Issued {quantity} × {component.name} from inventory")
    _settle_requests_for_component(component, 0 if via_request is not None else quantity, exclude=via_request)
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

    # …and it finishes on its own when the route was already done: the parts
    # were the only thing outstanding.
    from apps.assets.services import finish_build_if_done

    device.refresh_from_db()
    finish_build_if_done(device, user)


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

    # A line bought for a component opened in inventory is filed the way that
    # component was opened; the inspector is told, not asked.
    ordered_kind = None
    ordered_name = ""
    if po_item is not None and po_item.inventory_unit_type_id:
        ordered_kind, ordered_name = GoodsReceiptLine.Route.UNIQUE, po_item.inventory_unit_type.name
    elif po_item is not None and po_item.inventory_item_id:
        ordered_kind = GoodsReceiptLine.Route.GENERIC
        mt = po_item.inventory_item.material_type
        ordered_name = mt.name if mt is not None else po_item.inventory_item.sku
    if accepted_quantity and ordered_kind:
        if not route:
            route = ordered_kind
        elif route != ordered_kind:
            how = "unique items" if ordered_kind == GoodsReceiptLine.Route.UNIQUE else "generic stock"
            what = "the unique product" if ordered_kind == GoodsReceiptLine.Route.UNIQUE else "the stock item"
            raise serializers.ValidationError({
                "route": f"This line was bought for {what} '{ordered_name}' — it is filed as {how}.",
            })

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
                # A component carries its own category; the caller does not
                # get to disagree with it.
                from apps.assets.models import MaterialType

                own_category = MaterialType.objects.filter(
                    pk=material_type_id
                ).values_list("category_id", flat=True).first()
                inventory_item = InventoryItem.objects.create(
                    material_type_id=material_type_id,
                    category_id=own_category or generic.get("category"),
                    quantity=0,
                    min_stock_level=generic.get("min_stock_level", 5),
                    unit_cost=generic.get("unit_cost") or (po_item.unit_price if po_item else None),
                    storage_location=(generic.get("storage_location") or "").strip(),
                )
            elif (generic.get("storage_location") or "").strip():
                # Stock put somewhere new on this delivery: record where.
                inventory_item.storage_location = generic["storage_location"].strip()
            inventory_item.quantity += accepted_quantity
            inventory_item.save(update_fields=["quantity", "storage_location", "updated_at"])
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
                    # A part is received from its supplier: that is whose cover it carries.
                    warranty_type=(
                        "supplier"
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

    # Goods bought *for* a requirement land in stock like any other delivery.
    # The store hands them over against the material request the Procure
    # decision raised, and that issue — not the receipt — is what covers the
    # line on the project. Here those requests are only told the goods are in.
    ready_requests = []
    if accepted_quantity and po_item is not None:
        ready_requests = _requests_ready_to_issue(po_item, accepted_quantity, trace)

    # Stock bought against a reorder request has arrived: the request is done.
    if accepted_quantity and po_item is not None:
        po_item.reorder_requests.filter(status="ordered").update(status="received")

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
    return {"inventory_item": inventory_item, "units": created_units, "ready_requests": ready_requests}


def _requests_ready_to_issue(po_item, accepted_quantity, trace):
    """Tell the material requests waiting on this purchase line that the goods
    are in stock. Returns their numbers, so the inspector is pointed at them."""
    from django.utils import timezone

    from .models import IssuanceRequest

    rows = (
        IssuanceRequest.objects.select_for_update()
        .filter(asset_component__purchase_order_item=po_item, awaiting_procurement=True)
        .exclude(status__in=[IssuanceRequest.Status.CANCELLED, IssuanceRequest.Status.FULFILLED])
        .order_by("created_at")
    )
    stamp = timezone.localtime().strftime("%d %b %Y %H:%M")
    numbers = []
    for req in rows:
        req.notes = (req.notes + "\n" if req.notes else "") + (
            f"{stamp}: {accepted_quantity} received into stock ({trace}) — ready to issue."
        )
        req.save(update_fields=["notes", "updated_at"])
        numbers.append(req.request_number)
    return numbers


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
        component = request_row.asset_component
        if request_row.awaiting_procurement and component.outstanding_quantity > 0:
            po_item = component.purchase_order_item
            if po_item is None:
                raise serializers.ValidationError({"detail": (
                    f"{component.name} is to be procured and nothing has been ordered yet — "
                    "Procurement raises the purchase order first."
                )})
            if po_item.stocked_quantity <= 0:
                raise serializers.ValidationError({"detail": (
                    f"{component.name} is on {po_item.purchase_order.po_number} and nothing has been "
                    "received into stock yet — inspect the delivery first."
                )})
        if component.outstanding_quantity == 0:
            # Covered without passing through the queue: nothing left to hand
            # over, so the request closes rather than failing.
            request_row.status = IssuanceRequest.Status.CANCELLED
            request_row.notes = (request_row.notes + "\n" if request_row.notes else "") + (
                "Closed — the requirement was already covered from stock."
            )
            request_row.save(update_fields=["status", "notes", "updated_at"])
            return {
                "quantity": 0, "serials": [], "closed": True,
                "reason": (
                    f"{component.device.asset_code} · {component.name} is already fully covered "
                    f"({component.issued_quantity} of {component.quantity} issued) — nothing left to hand over, "
                    "so this request is closed."
                ),
            }
        if quantity > component.outstanding_quantity:
            raise serializers.ValidationError({
                "quantity": (
                    f"Only {component.outstanding_quantity} of this requirement is still outstanding on "
                    f"{component.device.asset_code} — issue that much."
                )
            })
        result = issue_stock_for_component(component, user, quantity, via_request=request_row)
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

    from django.utils import timezone as _tz

    now = _tz.now()
    request_row.quantity_issued += quantity
    request_row.issued_by = user
    request_row.last_issued_at = now
    if received_by:
        request_row.received_by = received_by
    if serials:
        request_row.issued_serials = [*(request_row.issued_serials or []), *serials]
    request_row.handovers = [*(request_row.handovers or []), {
        "at": now.isoformat(),
        "quantity": quantity,
        "received_by": received_by or "",
        "issued_by": (user.get_full_name() or user.username) if user else "",
        "serials": list(serials),
        "note": notes or "",
    }]
    request_row.sync_status()
    request_row.save(update_fields=[
        "quantity_issued", "issued_by", "last_issued_at", "received_by", "issued_serials", "handovers",
        "status", "updated_at",
    ])
    return {"quantity": quantity, "serials": serials}
