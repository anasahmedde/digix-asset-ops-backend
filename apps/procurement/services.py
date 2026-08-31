"""Goods receipt against a purchase order (WF-04).

One GRN records everything that arrived in a delivery: serialized lines
spawn one Device per captured serial number, consumable lines top up
warehouse stock. Everything runs in a single transaction — any validation
failure rolls the whole receipt back.
"""

from __future__ import annotations

from collections import Counter

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from apps.assets.models import Device
from apps.inventory.models import GoodsReceipt, GoodsReceiptLine, InventoryItem, StockMovement
from apps.teams.models import BOMAllocation

from .models import PurchaseOrder

RECEIVABLE_STATUSES = (
    PurchaseOrder.Status.ORDERED,
    PurchaseOrder.Status.PARTIALLY_RECEIVED,
)


def _validate_lines(purchase_order, lines):
    """Up-front validation of the whole payload — nothing is created unless
    every line passes. Returns the po_items keyed by pk."""
    po_items = {item.pk: item for item in purchase_order.items.select_for_update()}

    all_serials = []
    for index, line in enumerate(lines):
        label = f"lines[{index}]"
        po_item = po_items.get(line["po_item"])
        if po_item is None:
            raise serializers.ValidationError(
                {label: f"Item '{line['po_item']}' does not belong to this purchase order."}
            )
        line["_po_item"] = po_item

        remaining = po_item.quantity - po_item.received_quantity
        if line["quantity"] > remaining:
            raise serializers.ValidationError({
                label: (
                    f"Cannot receive {line['quantity']} of '{po_item.description}' — "
                    f"only {remaining} outstanding ({po_item.received_quantity} of "
                    f"{po_item.quantity} already received)."
                )
            })

        serials = [str(s).strip() for s in line.get("serial_numbers") or []]
        line["serial_numbers"] = serials
        if po_item.device_model_id:
            if len(serials) != line["quantity"]:
                raise serializers.ValidationError({
                    label: (
                        f"'{po_item.description}' is serialized: expected "
                        f"{line['quantity']} serial number(s), got {len(serials)}."
                    )
                })
            if any(not s for s in serials):
                raise serializers.ValidationError({label: "Serial numbers cannot be blank."})
        elif not po_item.material_type_id:
            raise serializers.ValidationError(
                {label: "line has no device model or material type"}
            )
        all_serials.extend(serials)

    # Serial uniqueness — within the payload and globally against Device.
    counts = Counter(all_serials)
    dupes_in_payload = [s for s, n in counts.items() if n > 1]
    existing = list(
        Device.objects.filter(serial_number__in=all_serials).values_list("serial_number", flat=True)
    )
    duplicates = sorted(set(dupes_in_payload) | set(existing))
    if duplicates:
        raise serializers.ValidationError({
            "serial_numbers": f"Serial number(s) already registered or repeated: {', '.join(duplicates)}."
        })

    return po_items


def receive_against_po(purchase_order, *, user, lines, reference="", notes=""):
    """Receive goods against ``purchase_order`` in one atomic step.

    ``lines`` = [{po_item: uuid, quantity: int, batch_number?: str,
    serial_numbers?: [str]}, …] (already type-validated by the serializer).

    Returns the response payload for the 201.
    """
    if purchase_order.status not in RECEIVABLE_STATUSES:
        raise serializers.ValidationError({
            "status": (
                f"Goods can only be received while the purchase order is Ordered or "
                f"Partially Received (currently '{purchase_order.get_status_display()}')."
            )
        })

    with transaction.atomic():
        po_items = _validate_lines(purchase_order, lines)

        receipt = GoodsReceipt.objects.create(
            purchase_order=purchase_order,
            reference=reference,
            notes=notes,
            received_by=user,
        )

        created_devices = []
        receipt_lines = []
        today = timezone.localdate()

        for line in lines:
            po_item = line["_po_item"]
            qty = line["quantity"]
            batch_number = line.get("batch_number", "")
            inventory_item = None

            if po_item.device_model_id:
                # Serialized: one Device per captured serial. DeviceModel has no
                # asset_type of its own, so the type comes from the PO line.
                bom_line = po_item.bom_line
                for serial in line["serial_numbers"]:
                    device = Device(
                        device_model=po_item.device_model,
                        asset_type=po_item.asset_type,
                        serial_number=serial,
                        batch_number=batch_number,
                        supplier=purchase_order.supplier,
                        purchase_price=po_item.unit_price,
                        purchase_date=today,
                        invoice_reference=receipt.grn_number,
                        source=Device.Source.THIRD_PARTY,
                        status=Device.Status.PROCURED,
                        project=bom_line.project if bom_line else None,
                    )
                    # Journalled as a 'Registered' lifecycle event by the
                    # Wave-1 signals; stash the actor so the journal names them.
                    device._transition_user = user
                    device.save()
                    created_devices.append(device)
                    if bom_line:
                        BOMAllocation.objects.create(
                            bom_line=bom_line,
                            device=device,
                            quantity=1,
                            status=BOMAllocation.Status.ALLOCATED,
                            allocated_by=user,
                        )
            else:
                # Consumable: top up (or open) the stock record for the material.
                inventory_item = (
                    InventoryItem.objects.select_for_update()
                    .filter(material_type=po_item.material_type)
                    .order_by("created_at")
                    .first()
                )
                if inventory_item is None:
                    inventory_item = InventoryItem.objects.create(
                        material_type=po_item.material_type,
                        quantity=0,
                        unit_cost=po_item.unit_price,
                    )
                inventory_item.quantity += qty
                inventory_item.save(update_fields=["quantity", "updated_at"])
                StockMovement.objects.create(
                    item=inventory_item,
                    movement_type=StockMovement.MovementType.IN,
                    quantity=qty,
                    reference=receipt.grn_number,
                    performed_by=user,
                    notes=f"Goods receipt {receipt.grn_number} against {purchase_order.po_number}",
                )

            receipt_lines.append(GoodsReceiptLine.objects.create(
                receipt=receipt,
                po_item=po_item,
                inventory_item=inventory_item,
                quantity=qty,
                batch_number=batch_number,
                serial_numbers=line["serial_numbers"],
            ))

            po_item.received_quantity += qty
            po_item.save(update_fields=["received_quantity", "updated_at"])

        # Auto-advance the PO — a system transition, not a user one: set the
        # status directly (bypassing the role-gated endpoint) and journal it
        # with the Wave-1 notes-append pattern tagged by GRN.
        fully_received = all(
            item.received_quantity >= item.quantity for item in po_items.values()
        )
        new_status = (
            PurchaseOrder.Status.RECEIVED if fully_received
            else PurchaseOrder.Status.PARTIALLY_RECEIVED
        )
        old_display = purchase_order.get_status_display()
        purchase_order.status = new_status
        stamp = timezone.localtime().strftime("%Y-%m-%d %H:%M")
        line = (
            f"[{stamp}] [GRN {receipt.grn_number}] "
            f"{old_display} → {purchase_order.get_status_display()}"
        )
        purchase_order.notes = (
            f"{purchase_order.notes}\n{line}" if purchase_order.notes else line
        )
        purchase_order.save(update_fields=["status", "notes", "updated_at"])

    from apps.inventory.serializers import GoodsReceiptLineSerializer

    return {
        "id": str(receipt.pk),
        "grn_number": receipt.grn_number,
        "purchase_order": str(purchase_order.pk),
        "created_devices": [
            {"id": str(d.pk), "asset_code": d.asset_code, "serial_number": d.serial_number}
            for d in created_devices
        ],
        "lines": GoodsReceiptLineSerializer(receipt_lines, many=True).data,
    }
