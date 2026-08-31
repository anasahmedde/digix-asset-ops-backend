from decimal import Decimal

from django.conf import settings
from django.db import models

from common.codes import generate_code
from common.models import TimeStampedModel


class PurchaseOrder(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING_APPROVAL = "pending_approval", "Pending Approval"
        APPROVED = "approved", "Approved"
        ORDERED = "ordered", "Ordered"
        PARTIALLY_RECEIVED = "partially_received", "Partially Received"
        RECEIVED = "received", "Received"
        CANCELLED = "cancelled", "Cancelled"

    class Currency(models.TextChoices):
        PKR = "PKR", "Pakistani Rupee"
        AED = "AED", "UAE Dirham"
        SAR = "SAR", "Saudi Riyal"
        QAR = "QAR", "Qatari Riyal"
        USD = "USD", "US Dollar"
        EUR = "EUR", "Euro"
        GBP = "GBP", "British Pound"

    po_number = models.CharField(max_length=50, unique=True, blank=True, db_index=True)
    supplier = models.ForeignKey(
        "suppliers.Supplier", on_delete=models.PROTECT, related_name="purchase_orders"
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    currency = models.CharField(max_length=3, choices=Currency.choices, default=Currency.PKR)
    order_date = models.DateField(null=True, blank=True)
    expected_delivery = models.DateField(null=True, blank=True)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    ordered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="purchase_orders"
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="approved_pos"
    )

    VALID_TRANSITIONS = {
        Status.DRAFT: (Status.PENDING_APPROVAL, Status.CANCELLED),
        Status.PENDING_APPROVAL: (Status.APPROVED, Status.DRAFT, Status.CANCELLED),
        Status.APPROVED: (Status.ORDERED, Status.CANCELLED),
        Status.ORDERED: (Status.PARTIALLY_RECEIVED, Status.RECEIVED, Status.CANCELLED),
        Status.PARTIALLY_RECEIVED: (Status.RECEIVED, Status.CANCELLED),
        Status.RECEIVED: (),
        Status.CANCELLED: (),
    }

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.po_number} - {self.supplier.name}"

    def save(self, *args, **kwargs):
        if not self.po_number:
            self.po_number = generate_code("purchase_order", model=type(self), field="po_number")
        super().save(*args, **kwargs)

    def can_transition_to(self, new_status: str) -> bool:
        return new_status in self.VALID_TRANSITIONS.get(self.status, ())

    def recalc_total(self, save: bool = True):
        total = sum((item.line_total for item in self.items.all()), Decimal("0"))
        self.total_amount = total
        if save:
            super().save(update_fields=["total_amount", "updated_at"])
        return total


class PurchaseOrderItem(TimeStampedModel):
    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.CASCADE, related_name="items"
    )
    asset_type = models.ForeignKey(
        "assets.AssetType", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    device_model = models.ForeignKey(
        "assets.DeviceModel", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    material_type = models.ForeignKey(
        "assets.MaterialType", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    bom_line = models.ForeignKey(
        "teams.ProjectBOMLine", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="po_items",
        help_text="Project BOM line this item was raised to cover (from-shortage flow)",
    )
    description = models.CharField(max_length=300)
    quantity = models.IntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    received_quantity = models.IntegerField(default=0)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.description} x{self.quantity}"

    @property
    def line_total(self):
        return self.quantity * self.unit_price
