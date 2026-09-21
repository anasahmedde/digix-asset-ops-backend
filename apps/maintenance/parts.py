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
