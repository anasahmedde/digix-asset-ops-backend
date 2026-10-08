"""Warranty lookups shared by tickets and maintenance (WF-14/15, MW-01/02)."""

from __future__ import annotations

from .models import Warranty

SUPPLIER_SIDE_TYPES = (
    Warranty.WarrantyType.SUPPLIER,
    Warranty.WarrantyType.MANUFACTURER,
    Warranty.WarrantyType.EXTENDED,
)


# A warranty with a claim in flight ("claimed"/Pending) still covers the
# asset — only expiry/void ends the cover (WF-15: expired → client pays).
LIVE_STATUSES = (Warranty.Status.ACTIVE, Warranty.Status.CLAIMED)


def expire_lapsed() -> int:
    """Mark every active warranty whose end date has gone by as completed.

    The six-hourly beat job does this, but a warranty must not read Active
    the day after it ends just because that job has not run - or is not
    running on a server - so every warranty read does it first. One indexed
    UPDATE, touching only rows that have actually lapsed.
    """
    from django.utils import timezone

    return Warranty.objects.filter(
        status=Warranty.Status.ACTIVE, end_date__lt=timezone.localdate(),
    ).update(status=Warranty.Status.EXPIRED, updated_at=timezone.now())


def get_active_client_warranty(device):
    """Newest live client-type warranty covering ``device``, or None."""
    if device is None:
        return None
    from django.utils import timezone

    # A lapsed date ends cover whatever the stored status still says.
    return (
        device.warranties.filter(
            warranty_type=Warranty.WarrantyType.CLIENT, status__in=LIVE_STATUSES,
            end_date__gte=timezone.localdate(),
        )
        .order_by("-end_date", "-created_at")
        .first()
    )


def get_active_supplier_warranty(device):
    """Newest live supplier-side warranty (supplier/manufacturer/extended), or None."""
    if device is None:
        return None
    from django.utils import timezone

    return (
        device.warranties.filter(
            warranty_type__in=SUPPLIER_SIDE_TYPES, status__in=LIVE_STATUSES,
            end_date__gte=timezone.localdate(),
        )
        .order_by("-end_date", "-created_at")
        .first()
    )


def derive_billability(device):
    """Default cost liability for service work on ``device``.

    Under an active client warranty the company bears the cost (or the vendor
    when a supplier-side warranty is also active); with no cover the client
    pays (WF-15). Returns (warranty, is_billable, charge_to).
    """
    client_warranty = get_active_client_warranty(device)
    if client_warranty is not None:
        supplier_warranty = get_active_supplier_warranty(device)
        charge_to = "vendor" if supplier_warranty is not None else "company"
        return client_warranty, False, charge_to
    return None, True, "client"


# -- Extending cover ---------------------------------------------------------
# A client warranty, a vendor's cover on an asset and a vendor's cover on a
# part are all extended the same way: a later expiry, the change written
# onto the record with who did it and on what reference.

def extended_expiry(current_end, data):
    """The expiry an extension asks for: a date, or months added to the current one."""
    from dateutil.relativedelta import relativedelta
    from django.utils.dateparse import parse_date
    from rest_framework import serializers

    raw_end, raw_months = data.get("end_date"), data.get("months")
    if raw_end:
        new_end = parse_date(str(raw_end))
        if new_end is None:
            raise serializers.ValidationError({"end_date": ["Use a real date."]})
    elif raw_months not in (None, ""):
        try:
            months = int(raw_months)
        except (TypeError, ValueError):
            raise serializers.ValidationError({"months": ["Must be a whole number."]})
        if months < 1:
            raise serializers.ValidationError({"months": ["Add at least one month."]})
        new_end = current_end + relativedelta(months=months)
    else:
        raise serializers.ValidationError(
            {"detail": "Give the new expiry date, or how many months to add."}
        )
    if new_end <= current_end:
        raise serializers.ValidationError({"end_date": [
            f"An extension has to move the expiry later than {current_end:%d %b %Y}."
        ]})
    return new_end


def term_months(start, end) -> int:
    """A start-to-end span in whole months, the way a warranty is quoted."""
    from dateutil.relativedelta import relativedelta

    delta = relativedelta(end, start)
    return max(1, delta.years * 12 + delta.months + (1 if delta.days else 0))


def extension_entry(old_end, new_end, user, data) -> str:
    """The line written onto the record: what moved, who moved it, on what."""
    who = user.get_full_name() or user.username
    entry = f"Extended {old_end:%d %b %Y} \u2192 {new_end:%d %b %Y} by {who}"
    reference = (data.get("reference_number") or "").strip()
    notes = (data.get("notes") or "").strip()
    if reference:
        entry += f" \u2014 ref {reference}"
    if notes:
        entry += f" \u2014 {notes}"
    return entry
