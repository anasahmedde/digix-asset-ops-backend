from django.conf import settings
from django.db import models

from common.codes import generate_code
from common.models import TimeStampedModel


class InventoryCategory(TimeStampedModel):
    """Consumables / Spare / PPE / Safety items / Tools / Stock items, …"""

    name = models.CharField(max_length=150, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "inventory categories"
        ordering = ["name"]

    def __str__(self):
        return self.name


class InventoryItem(TimeStampedModel):
    class Location(models.TextChoices):
        # Inventory lives in the warehouse or in transit — never "on site".
        # Assets are what live on site; consumption is modelled via Issuance.
        WAREHOUSE = "warehouse", "Warehouse"
        IN_TRANSIT = "in_transit", "In Transit"

    material_type = models.ForeignKey(
        "assets.MaterialType", on_delete=models.PROTECT, related_name="inventory_items"
    )
    category = models.ForeignKey(
        InventoryCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name="items"
    )
    sku = models.CharField(max_length=100, unique=True, blank=True, db_index=True)
    quantity = models.IntegerField(default=0)
    min_stock_level = models.IntegerField(default=5)
    location = models.CharField(max_length=15, choices=Location.choices, default=Location.WAREHOUSE)
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["material_type__name"]

    def __str__(self):
        return f"{self.material_type.name} ({self.sku})"

    def save(self, *args, **kwargs):
        if not self.sku:
            self.sku = generate_code("inventory_item", model=type(self), field="sku")
        super().save(*args, **kwargs)

    @property
    def is_low_stock(self):
        return self.quantity <= self.min_stock_level


class StockMovement(TimeStampedModel):
    class MovementType(models.TextChoices):
        IN = "in", "Stock In"
        OUT = "out", "Stock Out"
        TRANSFER = "transfer", "Transfer"
        ADJUSTMENT = "adjustment", "Adjustment"

    item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE, related_name="movements")
    movement_type = models.CharField(max_length=15, choices=MovementType.choices)
    quantity = models.IntegerField()
    reference = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="stock_movements"
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.movement_type} {self.quantity}x {self.item.material_type.name}"


class GoodsReceipt(TimeStampedModel):
    """Receiving stock into the warehouse — optionally against a Work Order
    (legacy single-item flow) or a Purchase Order (line-level GRN, WF-04)."""

    grn_number = models.CharField(max_length=50, unique=True, blank=True, db_index=True)
    work_order = models.ForeignKey(
        "workorders.WorkOrder", on_delete=models.SET_NULL, null=True, blank=True, related_name="goods_receipts"
    )
    purchase_order = models.ForeignKey(
        "procurement.PurchaseOrder", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="goods_receipts",
    )
    # Legacy single-item receipt fields; PO receipts use GoodsReceiptLine rows instead.
    item = models.ForeignKey(
        InventoryItem, on_delete=models.PROTECT, null=True, blank=True, related_name="receipts"
    )
    quantity = models.PositiveIntegerField(null=True, blank=True)
    reference = models.CharField(max_length=200, blank=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="goods_receipts"
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        target = self.item or self.purchase_order or self.work_order or "receipt"
        return f"{self.grn_number} - {target}"

    def save(self, *args, **kwargs):
        if not self.grn_number:
            self.grn_number = generate_code("goods_receipt", model=type(self), field="grn_number")
        super().save(*args, **kwargs)


class GoodsReceiptLine(TimeStampedModel):
    """One received PO line on a goods receipt: quantity plus the unique
    serials/batch captured at the door (WF-04)."""

    receipt = models.ForeignKey(GoodsReceipt, on_delete=models.CASCADE, related_name="lines")
    po_item = models.ForeignKey(
        "procurement.PurchaseOrderItem", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="receipt_lines",
    )
    inventory_item = models.ForeignKey(
        InventoryItem, on_delete=models.SET_NULL, null=True, blank=True, related_name="receipt_lines"
    )
    quantity = models.PositiveIntegerField()
    batch_number = models.CharField(max_length=100, blank=True)
    serial_numbers = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.receipt.grn_number}: {self.po_item or self.inventory_item} x{self.quantity}"


class Issuance(TimeStampedModel):
    """Issuing stock out of the warehouse to a site / work order / technician."""

    issue_number = models.CharField(max_length=50, unique=True, blank=True, db_index=True)
    item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name="issuances")
    quantity = models.PositiveIntegerField()
    issued_to_site = models.ForeignKey(
        "sites.Site", on_delete=models.SET_NULL, null=True, blank=True, related_name="inventory_issuances"
    )
    issued_to_work_order = models.ForeignKey(
        "workorders.WorkOrder", on_delete=models.SET_NULL, null=True, blank=True, related_name="inventory_issuances"
    )
    issued_to_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="received_issuances"
    )
    issued_to_project = models.ForeignKey(
        "teams.Project", on_delete=models.SET_NULL, null=True, blank=True, related_name="inventory_issuances"
    )
    bom_line = models.ForeignKey(
        "teams.ProjectBOMLine", on_delete=models.SET_NULL, null=True, blank=True, related_name="issuances"
    )
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="issued_issuances"
    )
    reason = models.CharField(max_length=300, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.issue_number} - {self.item}"

    def save(self, *args, **kwargs):
        if not self.issue_number:
            self.issue_number = generate_code("issuance", model=type(self), field="issue_number")
        super().save(*args, **kwargs)
