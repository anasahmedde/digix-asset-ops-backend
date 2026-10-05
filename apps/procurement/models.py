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
    # The supplier's particulars for this order — a contact, a quote
    # reference, a delivery address — where they differ from the supplier's
    # standing record. Printed on the order under the supplier.
    supplier_details = models.TextField(blank=True)
    # Printed on the order the supplier receives. Seeded from the house
    # standard, then edited per order when a deal says something different.
    terms = models.TextField(blank=True)
    ordered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="purchase_orders"
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="approved_pos"
    )

    VALID_TRANSITIONS = {
        Status.DRAFT: (Status.PENDING_APPROVAL, Status.CANCELLED),
        Status.PENDING_APPROVAL: (Status.APPROVED, Status.DRAFT, Status.CANCELLED),
        # Approval places the order — the Group Head's signature is what
        # commits the company — so an approved order is received against
        # directly. "Ordered" remains only for rows that reached it before.
        Status.APPROVED: (Status.PARTIALLY_RECEIVED, Status.RECEIVED, Status.CANCELLED),
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

    def unagreed_lines(self):
        """Lines priced over their reference that nobody has yet agreed to.

        An order carrying one of these must not reach the Group Head: the
        signature commits the company, and the figure it commits to was never
        the one anybody planned for.
        """
        return self.items.filter(
            variance_status__in=(
                PurchaseOrderItem.VarianceStatus.PENDING,
                PurchaseOrderItem.VarianceStatus.REJECTED,
            )
        )

    def recalc_total(self, save: bool = True):
        total = sum((item.line_total for item in self.items.all()), Decimal("0"))
        self.total_amount = total
        if save:
            super().save(update_fields=["total_amount", "updated_at"])
        return total


class PurchaseOrderItem(TimeStampedModel):
    """One line on an order, and whether its price has been agreed.

    A line raised from a request carries the figure it was expected to cost
    — what the project's budget was approved on, or what the store last paid.
    Paying more than that is somebody else's money, so the side that owns the
    figure has to say yes before the order can go up for signature.
    """

    class VarianceOwner(models.TextChoices):
        PROJECT = "project", "Project Execution"
        INVENTORY = "inventory", "Inventory"
        # Nothing was planned and no shelf is being filled — a vendor asset
        # bought on its own. Operations owns the call.
        OPERATIONS = "operations", "Operations Head"

    class VarianceStatus(models.TextChoices):
        NOT_REQUIRED = "not_required", "Within reference"
        PENDING = "pending", "Awaiting approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.CASCADE, related_name="items"
    )
    asset_type = models.ForeignKey(
        "assets.AssetType", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    device_model = models.ForeignKey(
        "assets.DeviceModel", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # The exact stock record this line replenishes. Set when the line was
    # raised from an asset requirement, so the goods land on the row the
    # requirement is watching rather than on some other row for the same
    # material.
    inventory_item = models.ForeignKey(
        "inventory.InventoryItem", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="purchase_order_items",
    )
    # A line can target an opened unique product directly: everything the
    # goods need is already on that record, so receiving them only needs
    # serial numbers.
    inventory_unit_type = models.ForeignKey(
        "inventory.InventoryUnitType", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="purchase_order_items",
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
    # Delivery, installation, a service fee: money on the order that is not
    # goods. Nothing arrives at the door for it, so receiving skips it and the
    # order can complete without it.
    is_charge = models.BooleanField(default=False)

    # ── the price this line was expected to come in at ────────────────
    # Frozen when the order is raised: the reference moves every time
    # something is bought, and what was approved must not move with it.
    reference_unit_price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    reference_label = models.CharField(max_length=120, blank=True)
    variance_owner = models.CharField(
        max_length=12, choices=VarianceOwner.choices, blank=True
    )
    variance_status = models.CharField(
        max_length=14, choices=VarianceStatus.choices,
        default=VarianceStatus.NOT_REQUIRED, db_index=True,
    )
    # Why the buyer is paying over the odds, and what the owner said back.
    variance_reason = models.TextField(blank=True)
    variance_notes = models.TextField(blank=True)
    variance_decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="price_variance_decisions",
    )
    variance_decided_at = models.DateTimeField(null=True, blank=True)
    # The figure that was actually agreed. A decision is about a number, not
    # about a row, so moving the number undoes the decision.
    variance_decided_price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.description} x{self.quantity}"

    @property
    def line_total(self):
        return self.quantity * self.unit_price

    @property
    def over_reference(self) -> bool:
        """Is this line priced above what it was expected to cost?"""
        return (
            self.reference_unit_price is not None
            and self.unit_price is not None
            and self.unit_price > self.reference_unit_price
        )

    @property
    def variance_percent(self):
        """How far over, as a percentage. None when there is nothing to compare."""
        if not self.over_reference or not self.reference_unit_price:
            return None
        return (self.unit_price - self.reference_unit_price) / self.reference_unit_price * 100

    @property
    def blocks_submission(self) -> bool:
        return self.variance_status in (
            self.VarianceStatus.PENDING, self.VarianceStatus.REJECTED
        )

    def sync_variance(self):
        """Keep the approval in step with the price on the line.

        Priced over the reference and the owner has to bless it; priced back
        within it and there is nothing left to bless. Changing an agreed price
        undoes the agreement, because what was agreed was a figure.
        """
        if self.reference_unit_price is None or self.unit_price is None:
            return
        if not self.over_reference:
            self.variance_status = self.VarianceStatus.NOT_REQUIRED
            return
        decided = self.variance_status in (
            self.VarianceStatus.APPROVED, self.VarianceStatus.REJECTED
        )
        if self.variance_status == self.VarianceStatus.NOT_REQUIRED or (
            decided and self.variance_decided_price != self.unit_price
        ):
            self.variance_status = self.VarianceStatus.PENDING
            self.variance_decided_by = None
            self.variance_decided_at = None
            self.variance_decided_price = None
            self.variance_notes = ""

    def save(self, *args, **kwargs):
        fields = kwargs.get("update_fields")
        # A save that does not touch the price cannot change the agreement.
        if fields is None or "unit_price" in fields or "reference_unit_price" in fields:
            self.sync_variance()
            if fields is not None:
                kwargs["update_fields"] = list(dict.fromkeys(
                    list(fields) + [
                        "variance_status", "variance_decided_by", "variance_decided_at",
                        "variance_decided_price", "variance_notes",
                    ]
                ))
        super().save(*args, **kwargs)

    @property
    def stocked_quantity(self) -> int:
        """How much of this line has passed inspection into stock.

        Receiving only queues goods; inspection is what puts them on the shelf,
        so this — not ``received_quantity`` — says whether the store can issue.
        """
        return sum(
            (line.accepted_quantity or 0)
            for line in self.receipt_lines.all()
            if line.inspection_status == "passed"
        )
