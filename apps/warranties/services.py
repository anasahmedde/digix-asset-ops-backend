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


def get_active_client_warranty(device):
    """Newest live client-type warranty covering ``device``, or None."""
    if device is None:
        return None
    return (
        device.warranties.filter(
            warranty_type=Warranty.WarrantyType.CLIENT, status__in=LIVE_STATUSES
        )
        .order_by("-end_date", "-created_at")
        .first()
    )


def get_active_supplier_warranty(device):
    """Newest live supplier-side warranty (supplier/manufacturer/extended), or None."""
    if device is None:
        return None
    return (
        device.warranties.filter(
            warranty_type__in=SUPPLIER_SIDE_TYPES, status__in=LIVE_STATUSES
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
