"""
Work Orders — distinct from Tickets.

Per the domain: **Tickets** are complaints/faults on already-installed assets,
while **Work Orders** cover installing or purchasing *new* assets (issued to a
supplier). A Work Order carries asset line items, supplier, payment terms,
terms & conditions, warranty period, timelines and safety instructions, and can
be printed as a PDF for the supplier.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models

from common.codes import generate_code
from common.models import TimeStampedModel


class WorkOrder(TimeStampedModel):
    class OrderType(models.TextChoices):
        # What a work order is: services a vendor performs for us — an
        # operation of a build given to a workshop, installation labour, a
        # repair. Goods are bought on purchase orders, not here.
        SERVICES = "services", "Services"
        SUPPLY = "supply", "Supply / Purchase"
        INSTALLATION = "installation", "Installation"
        SUPPLY_INSTALL = "supply_install", "Supply & Installation"
        PRODUCTION = "production", "Production Step"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING_APPROVAL = "pending_approval", "Pending Approval"
        APPROVED = "approved", "Approved"
        ISSUED = "issued", "Issued to Supplier"
        IN_PROGRESS = "in_progress", "In Progress"
        PARTIALLY_DELIVERED = "partially_delivered", "Partially Delivered"
        DELIVERED = "delivered", "Delivered — awaiting inspection"
        COMPLETED = "completed", "Completed — inspected"
        CANCELLED = "cancelled", "Cancelled"

    class Currency(models.TextChoices):
        PKR = "PKR", "Pakistani Rupee"
        AED = "AED", "UAE Dirham"
        SAR = "SAR", "Saudi Riyal"
        QAR = "QAR", "Qatari Riyal"
        USD = "USD", "US Dollar"
        EUR = "EUR", "Euro"
        GBP = "GBP", "British Pound"

    wo_number = models.CharField(max_length=50, unique=True, blank=True, db_index=True)
    title = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    order_type = models.CharField(max_length=20, choices=OrderType.choices, default=OrderType.SERVICES)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)

    supplier = models.ForeignKey(
        "suppliers.Supplier", on_delete=models.PROTECT, related_name="work_orders"
    )
    client = models.ForeignKey(
        "clients.Client", on_delete=models.SET_NULL, null=True, blank=True, related_name="work_orders"
    )
    site = models.ForeignKey(
        "sites.Site", on_delete=models.SET_NULL, null=True, blank=True, related_name="work_orders"
    )

    # Raised from a project's execution for a vendor-built asset, the way a PO
    # is raised for a component. Its cost is that asset's actual cost.
    project = models.ForeignKey(
        "teams.Project", on_delete=models.SET_NULL, null=True, blank=True, related_name="work_orders"
    )
    device = models.ForeignKey(
        "assets.Device", on_delete=models.SET_NULL, null=True, blank=True, related_name="work_orders"
    )
    # One operation of an in-house build given to an outside workshop.
    production_step = models.ForeignKey(
        "assets.ProductionStep", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="work_orders",
    )
    payment_terms = models.ForeignKey(
        "setup.PaymentTerms", on_delete=models.SET_NULL, null=True, blank=True, related_name="work_orders"
    )
    terms_template = models.ForeignKey(
        "setup.TermsTemplate", on_delete=models.SET_NULL, null=True, blank=True, related_name="work_orders"
    )
    terms_conditions = models.TextField(blank=True, help_text="Overrides the template if provided")
    safety_instructions = models.TextField(blank=True)
    warranty_months = models.PositiveSmallIntegerField(null=True, blank=True)

    currency = models.CharField(max_length=3, choices=Currency.choices, default=Currency.PKR)
    order_date = models.DateField(null=True, blank=True)
    expected_delivery = models.DateField(null=True, blank=True)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    notes = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="created_work_orders"
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="approved_work_orders"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    # Work receiving: the vendor delivers, we inspect. Accepted work completes
    # the order; work sent back for rework goes to the vendor again.
    class InspectionResult(models.TextChoices):
        ACCEPTED = "accepted", "Accepted"
        REWORK = "rework", "Sent back for rework"

    inspected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="inspected_work_orders"
    )
    inspected_at = models.DateTimeField(null=True, blank=True)
    inspection_result = models.CharField(max_length=10, choices=InspectionResult.choices, blank=True)
    inspection_notes = models.TextField(blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    issued_at = models.DateTimeField(null=True, blank=True)

    VALID_TRANSITIONS = {
        Status.DRAFT: (Status.PENDING_APPROVAL, Status.CANCELLED),
        Status.PENDING_APPROVAL: (Status.APPROVED, Status.DRAFT, Status.CANCELLED),
        Status.APPROVED: (Status.ISSUED, Status.CANCELLED),
        Status.ISSUED: (Status.IN_PROGRESS, Status.CANCELLED),
        Status.IN_PROGRESS: (Status.PARTIALLY_DELIVERED, Status.DELIVERED, Status.CANCELLED),
        # A vendor can keep sending jobs in one at a time, so a part delivery
        # can follow a part delivery until the last job is in.
        Status.PARTIALLY_DELIVERED: (Status.PARTIALLY_DELIVERED, Status.DELIVERED, Status.CANCELLED),
        Status.DELIVERED: (Status.COMPLETED,),
        Status.COMPLETED: (),
        Status.CANCELLED: (),
    }

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["wo_number"]),
        ]

    def __str__(self):
        return f"{self.wo_number} - {self.title}"

    def save(self, *args, **kwargs):
        if not self.wo_number:
            self.wo_number = generate_code("work_order", model=type(self), field="wo_number")
        super().save(*args, **kwargs)

    def can_transition_to(self, new_status: str) -> bool:
        return new_status in self.VALID_TRANSITIONS.get(self.status, ())

    def recalc_total(self, save: bool = True):
        total = sum((item.line_total for item in self.items.all()), Decimal("0"))
        self.total_amount = total
        if save:
            super().save(update_fields=["total_amount", "updated_at"])
        return total


class WorkOrderItem(TimeStampedModel):
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="items")
    asset_type = models.ForeignKey(
        "assets.AssetType", on_delete=models.SET_NULL, null=True, blank=True, related_name="work_order_items"
    )
    device_model = models.ForeignKey(
        "assets.DeviceModel", on_delete=models.SET_NULL, null=True, blank=True, related_name="work_order_items"
    )
    # The operation of a build this line pays for, when the order came from a
    # production route. One order can carry several operations for one vendor.
    production_step = models.ForeignKey(
        "assets.ProductionStep", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="work_order_items",
    )
    description = models.CharField(max_length=300)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    received_quantity = models.PositiveIntegerField(default=0)

    # A vendor with three jobs on one order can finish one and send it in while
    # the others are still on his bench, so each line carries its own delivery
    # and its own verdict. The order follows its lines.
    delivered_at = models.DateTimeField(null=True, blank=True)
    inspected_at = models.DateTimeField(null=True, blank=True)
    inspected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="work_order_lines_inspected",
    )
    inspection_result = models.CharField(
        max_length=10, choices=WorkOrder.InspectionResult.choices, blank=True
    )
    inspection_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.description} x{self.quantity}"

    @property
    def line_total(self) -> Decimal:
        return (self.unit_price or Decimal("0")) * self.quantity

    @property
    def accepted(self) -> bool:
        """The work on this line was inspected and passed — it is finished."""
        return self.inspection_result == WorkOrder.InspectionResult.ACCEPTED

    @property
    def awaiting_inspection(self) -> bool:
        """It has come in and nobody has looked at it yet."""
        return self.delivered_at is not None and not self.accepted

    @property
    def with_vendor(self) -> bool:
        """Still out: never delivered, or sent back to be redone."""
        return self.delivered_at is None and not self.accepted

    @property
    def line_state(self) -> str:
        if self.accepted:
            return "accepted"
        if self.delivered_at is not None:
            return "awaiting_inspection"
        return "rework" if self.inspection_result else "with_vendor"
