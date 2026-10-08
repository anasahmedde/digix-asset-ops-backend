"""Paying more than was planned, and who has to agree to it.

A line raised from a request carries the figure it was expected to cost —
what the project's budget was approved on, or what the store last paid.
Going above it is somebody else's money, so the side that owns the figure
says yes before the order can go up for signature.

Purchases and work orders answer to the same rule, so they answer to the
same code: one mixin, two line models, no drift between them.
"""

from django.conf import settings
from django.db import models


class PriceVariance(models.Model):
    """The price a line was expected to come in at, and the agreement to pass it."""

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
        related_name="%(app_label)s_%(class)s_variance_decisions",
    )
    variance_decided_at = models.DateTimeField(null=True, blank=True)
    # The figure that was actually agreed. A decision is about a number, not
    # about a row, so moving the number undoes the decision.
    variance_decided_price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )

    class Meta:
        abstract = True

    @property
    def parent_order(self):
        """The order this line sits on, whichever kind it is."""
        return getattr(self, "purchase_order", None) or getattr(self, "work_order", None)

    @property
    def order_number(self) -> str:
        order = self.parent_order
        return getattr(order, "po_number", "") or getattr(order, "wo_number", "") or ""

    @property
    def order_raised_by(self):
        """Who raised it — a purchase order says ordered_by, a work order created_by."""
        order = self.parent_order
        return getattr(order, "ordered_by", None) or getattr(order, "created_by", None)

    @property
    def project_of_line(self):
        """The project whose money this line spends, where there is one.

        A purchase line points at it through the requirement it was raised
        for; a work-order line through the operation, or the order itself.
        The project's manager agrees their own project's variances.
        """
        from apps.assets.serializers import _project_of

        order = self.parent_order
        if getattr(order, "project_id", None):
            return order.project
        bom = getattr(self, "bom_line", None)
        if bom is not None and bom.project_id:
            return bom.project
        step = getattr(self, "production_step", None)
        if step is not None and step.device_id:
            return _project_of(step.device)
        for rel in ("asset_components", "procured_devices"):
            manager = getattr(self, rel, None)
            if manager is None:
                continue
            first = manager.all().first()
            if first is None:
                continue
            device = getattr(first, "device", first)
            project = _project_of(device)
            if project is not None:
                return project
        return None

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
