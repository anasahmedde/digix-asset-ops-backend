"""Goods receipt against a purchase order (WF-04).

One GRN records everything that arrived in a delivery. Receipt does **not**
put anything into the warehouse: every line is queued ``pending`` inspection.
A technician then inspects it and routes the accepted quantity into generic
stock or unique units (``inventory.services.stock_inspected_line``), which is
the only step that moves stock. Everything runs in a single transaction — any
validation failure rolls the whole receipt back.
"""

from __future__ import annotations

from collections import Counter

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from apps.assets.models import Device
from apps.inventory.models import GoodsReceipt, GoodsReceiptLine

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
        if po_item.procured_devices.exists():
            # A complete asset bought from a vendor. The registry issued its
            # asset code when it was defined, and that is what identifies it,
            # so there is no serial to collect at the door.
            line["serial_numbers"] = serials = []
        # Serialized either because the line names a device model or because it
        # names an opened unique product.
        elif po_item.device_model_id or po_item.inventory_unit_type_id:
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
                {label: "line has no unique product, device model or material type"}
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

        receipt_lines = []

        for line in lines:
            po_item = line["_po_item"]
            qty = line["quantity"]
            batch_number = line.get("batch_number", "")

            # Goods stop at the door. Nothing is stocked and no asset is
            # created here — the line waits for a technician's inspection,
            # which routes the accepted quantity into generic stock or unique
            # units (see inventory.services.stock_inspected_line).
            receipt_lines.append(GoodsReceiptLine.objects.create(
                receipt=receipt,
                po_item=po_item,
                quantity=qty,
                batch_number=batch_number,
                serial_numbers=line["serial_numbers"],
                inspection_status=GoodsReceiptLine.Inspection.PENDING,
            ))

            po_item.received_quantity += qty
            po_item.save(update_fields=["received_quantity", "updated_at"])

            # A line that buys complete assets is not stock to inspect: the
            # assets already exist in the registry and now physically arrive.
            # They come into stock, priced at what the order paid, with the
            # vendor's warranty running from today if a term was given.
            arriving = list(po_item.procured_devices.filter(status="procured")[:qty])
            if arriving:
                from dateutil.relativedelta import relativedelta

                from apps.warranties.models import Warranty

                today = timezone.localdate()
                months = line.get("warranty_months")
                for device in arriving:
                    device._transition_user = user
                    device._transition_reason = f"Received against {purchase_order.po_number}"
                    device.status = "in_stock"
                    device.purchase_date = today
                    device.purchase_price = po_item.unit_price
                    device.supplier = purchase_order.supplier
                    device.save(update_fields=[
                        "status", "purchase_date", "purchase_price", "supplier", "updated_at",
                    ])
                    if months:
                        Warranty.objects.create(
                            device=device,
                            supplier=purchase_order.supplier,
                            warranty_type="supplier",
                            status="active",
                            start_date=today,
                            end_date=today + relativedelta(months=int(months)),
                            months=int(months),
                        )
                receipt_lines[-1].inspection_status = GoodsReceiptLine.Inspection.PASSED
                receipt_lines[-1].inspection_notes = "Complete asset — entered the registry directly."
                receipt_lines[-1].inspected_by = user
                receipt_lines[-1].inspected_at = timezone.now()
                receipt_lines[-1].save(update_fields=[
                    "inspection_status", "inspection_notes", "inspected_by", "inspected_at", "updated_at",
                ])

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
        # Received goods are held for inspection — nothing is in stock yet.
        "pending_inspection": len(receipt_lines),
        "created_devices": [],
        "lines": GoodsReceiptLineSerializer(receipt_lines, many=True).data,
    }
