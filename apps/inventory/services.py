"""Stock-side effects shared by every issuance path (direct site issuance
and BOM-line issue). Callers must wrap in transaction.atomic()."""

from .models import InventoryItem, StockMovement


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
