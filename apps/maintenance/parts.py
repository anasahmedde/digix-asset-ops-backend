"""Turning an approved parts line into a request on the store's queue.

Approving does not issue anything. It puts the agreed quantity in front of the
store, which is the only place material leaves from, and links the two so the
job can say where its parts have got to.
"""
from __future__ import annotations

from django.utils import timezone
from rest_framework import serializers


def decide(part_request, *, user, approve, quantity=None, note=""):
    """Answer one line, and raise the store request when it is approved.

    ``quantity`` is what is actually being released; left out, the whole
    amount asked for is agreed. It can be cut but never raised — approving
    more than was asked for is somebody else's decision to make.
    """
    from apps.inventory.models import IssuanceRequest

    from .models import MaintenancePartRequest

    if part_request.status != MaintenancePartRequest.Status.REQUESTED:
        raise serializers.ValidationError(
            {"status": f"This line was already {part_request.get_status_display().lower()}."}
        )

    part_request.decided_by = user
    part_request.decided_at = timezone.now()
    part_request.decision_note = note or ""

    if not approve:
        part_request.status = MaintenancePartRequest.Status.REJECTED
        part_request.quantity_approved = 0
        part_request.save(update_fields=[
            "status", "quantity_approved", "decided_by", "decided_at",
            "decision_note", "updated_at",
        ])
        return part_request

    agreed = part_request.quantity_requested if quantity is None else int(quantity)
    if agreed > part_request.quantity_requested:
        raise serializers.ValidationError({
            "quantity": (
                f"{part_request.what} was asked for {part_request.quantity_requested} "
                f"{part_request.unit}; approve that or less."
            )
        })
    if agreed < 1:
        raise serializers.ValidationError(
            {"quantity": "Approve at least one, or reject the line."}
        )

    schedule = part_request.schedule
    issuance = IssuanceRequest.objects.create(
        item=part_request.item,
        unit_type=part_request.unit_type,
        quantity_requested=agreed,
        source=IssuanceRequest.Source.MAINTENANCE,
        maintenance_schedule=schedule,
        purpose=f"{schedule.title}"
                + (f" · {schedule.device.asset_code}" if schedule.device_id else ""),
        requested_by=part_request.requested_by,
    )
    part_request.status = MaintenancePartRequest.Status.APPROVED
    part_request.quantity_approved = agreed
    part_request.issuance_request = issuance
    part_request.save(update_fields=[
        "status", "quantity_approved", "issuance_request", "decided_by",
        "decided_at", "decision_note", "updated_at",
    ])
    return part_request


def settle(schedule, *, user, rows, visit=None):
    """Say what the visit did with the parts the store issued for it.

    Each row is ``{"part_request": id, "used": int, "serials": [...]}``, and
    ``visit`` is the round being closed out, which the lines are stamped with
    so a schedule running every month can say which round used what.
    Whatever was issued and not used goes back to the store in one return
    receipt, which waits in receiving to be inspected like any delivery —
    a technician saying a part is unused does not put it back on the shelf.

    Returns the receipt, or ``None`` when nothing came back.
    """
    from apps.inventory.models import GoodsReceipt
    from apps.inventory.returns import record_return

    lines = {str(p.pk): p for p in schedule.part_requests.select_related("issuance_request").all()}
    settled, coming_back = [], []

    for row in rows or []:
        line = lines.get(str(row.get("part_request")))
        if line is None:
            raise serializers.ValidationError(
                {"parts_settlement": ["That parts line belongs to another job."]}
            )
        issued = line.issuance_request.quantity_issued if line.issuance_request_id else 0
        if issued < 1:
            raise serializers.ValidationError({
                "parts_settlement": [f"Nothing has been issued for {line.what} yet."]
            })
        try:
            used = int(row.get("used"))
        except (TypeError, ValueError):
            raise serializers.ValidationError({
                "parts_settlement": [f"Say how much of {line.what} the visit used."]
            })
        if used < 0 or used > issued:
            raise serializers.ValidationError({
                "parts_settlement": [
                    f"{line.what}: {issued} {line.unit} was issued, so between 0 and {issued} was used."
                ]
            })

        returning = issued - used
        serials = [str(x).strip() for x in (row.get("serials") or []) if str(x).strip()]
        if returning and line.unit_type_id:
            issued_serials = list(line.issuance_request.issued_serials or [])
            if len(serials) != returning:
                raise serializers.ValidationError({
                    "parts_settlement": [
                        f"{line.what}: name the {returning} unit(s) coming back, by serial number."
                    ]
                })
            stray = [s for s in serials if issued_serials and s not in issued_serials]
            if stray:
                raise serializers.ValidationError({
                    "parts_settlement": [
                        f"{line.what}: {', '.join(stray)} was not issued on this job."
                    ]
                })

        line.quantity_used = used
        line.quantity_returned = returning
        line.visit = visit or line.visit
        settled.append(line)
        if returning:
            coming_back.append({
                "item": line.item_id,
                "unit_type": line.unit_type_id,
                "quantity": returning,
                "serials": serials,
            })

    receipt = None
    if coming_back:
        asset = schedule.device.asset_code if schedule.device_id else ""
        receipt = record_return(
            source=GoodsReceipt.Source.MAINTENANCE_RETURN,
            rows=coming_back,
            reference=" · ".join(x for x in [schedule.title, asset] if x),
            notes="Left over from a maintenance visit.",
            user=user,
        )
    for line in settled:
        line.return_reference = receipt.grn_number if receipt and line.quantity_returned else ""
        line.save(update_fields=[
            "visit", "quantity_used", "quantity_returned", "return_reference", "updated_at",
        ])
    return receipt
