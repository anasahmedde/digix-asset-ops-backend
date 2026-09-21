"""Material coming back to the store.

Leftovers from a project build and the parts a maintenance visit did not use
come back the same way a delivery arrives: a receipt whose lines wait for
inspection, so nothing re-enters stock unchecked. One receipt can carry
several lines, because one job hands back everything it has left in one go.
"""
from __future__ import annotations

from django.db import transaction
from rest_framework import serializers

from .models import GoodsReceipt, GoodsReceiptLine

RETURN_SOURCES = (
    GoodsReceipt.Source.PROJECT_RETURN,
    GoodsReceipt.Source.MAINTENANCE_RETURN,
)


def record_return(*, source, rows, reference="", notes="", user=None):
    """Book material back in, pending inspection.

    Each row is ``{"item": id|None, "unit_type": id|None, "quantity": int,
    "serials": [...]}`` — generic stock or a unique product, exactly as the
    rest of the system names what it is talking about. Returns the receipt.
    """
    if source not in RETURN_SOURCES:
        raise serializers.ValidationError(
            {"source": ["Say whether this is a project or a maintenance return."]}
        )

    cleaned = []
    for row in rows:
        try:
            quantity = int(row.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        if quantity < 1:
            raise serializers.ValidationError({"quantity": ["Return at least one."]})
        item_id = row.get("item") or None
        unit_type_id = row.get("unit_type") or None
        if not item_id and not unit_type_id:
            raise serializers.ValidationError(
                {"inventory_item": ["Name the component coming back."]}
            )
        serials = list(row.get("serials") or [])
        # A unique unit is known by its serial: without one there is no saying
        # which of them is back on the shelf.
        if unit_type_id and len(serials) != quantity:
            raise serializers.ValidationError(
                {"serial_numbers": [f"Give {quantity} serial number(s) for the units coming back."]}
            )
        cleaned.append((item_id, unit_type_id, quantity, serials))

    with transaction.atomic():
        receipt = GoodsReceipt.objects.create(
            source=source,
            reference=(reference or "").strip(),
            notes=(notes or "").strip(),
            received_by=user,
        )
        for item_id, unit_type_id, quantity, serials in cleaned:
            GoodsReceiptLine.objects.create(
                receipt=receipt,
                inventory_item_id=item_id,
                quantity=quantity,
                serial_numbers=serials,
                inspection_status=GoodsReceiptLine.Inspection.PENDING,
                # A unique product is remembered on the line's notes for the
                # inspector, who files the serials against it.
                inspection_notes=f"unit_type:{unit_type_id}" if unit_type_id else "",
            )
    return receipt
