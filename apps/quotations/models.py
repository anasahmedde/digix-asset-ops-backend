"""
Quotations — the commercial front door of the workflow (WF-01).

A Quotation is priced line-item offer to a client. It moves through a small
machine (draft → sent → under negotiation → accepted / rejected / expired);
on acceptance it spawns the delivery :class:`apps.teams.models.Project` at the
order-confirmation phase and copies its items onto the project's BOM.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models

from common.codes import generate_code
from common.models import TimeStampedModel


class Quotation(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent"
        UNDER_NEGOTIATION = "under_negotiation", "Under Negotiation"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        EXPIRED = "expired", "Expired"

    class Currency(models.TextChoices):
        # Same currency choices as WorkOrder / PurchaseOrder.
        PKR = "PKR", "Pakistani Rupee"
        AED = "AED", "UAE Dirham"
        SAR = "SAR", "Saudi Riyal"
        QAR = "QAR", "Qatari Riyal"
        USD = "USD", "US Dollar"
        EUR = "EUR", "Euro"
        GBP = "GBP", "British Pound"

    quote_number = models.CharField(max_length=50, unique=True, blank=True, db_index=True)
    title = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)

    client = models.ForeignKey(
        "clients.Client", on_delete=models.PROTECT, related_name="quotations"
    )
    site = models.ForeignKey(
        "sites.Site", on_delete=models.SET_NULL, null=True, blank=True, related_name="quotations"
    )

    currency = models.CharField(max_length=3, choices=Currency.choices, default=Currency.PKR)
    valid_until = models.DateField(null=True, blank=True)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    notes = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="created_quotations"
    )
    accepted_at = models.DateTimeField(null=True, blank=True)

    VALID_TRANSITIONS = {
        Status.DRAFT: (Status.SENT,),
        Status.SENT: (Status.UNDER_NEGOTIATION, Status.ACCEPTED, Status.REJECTED, Status.EXPIRED),
        Status.UNDER_NEGOTIATION: (Status.ACCEPTED, Status.REJECTED, Status.EXPIRED),
        Status.ACCEPTED: (),
        Status.REJECTED: (),
        Status.EXPIRED: (),
    }

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["quote_number"]),
        ]

    def __str__(self):
        return f"{self.quote_number} - {self.title}"

    def save(self, *args, **kwargs):
        if not self.quote_number:
            self.quote_number = generate_code("quotation", model=type(self), field="quote_number")
        super().save(*args, **kwargs)

    def can_transition_to(self, new_status: str) -> bool:
        return new_status in self.VALID_TRANSITIONS.get(self.status, ())

    def recalc_total(self, save: bool = True):
        total = sum((item.line_total for item in self.items.all()), Decimal("0"))
        self.total_amount = total
        if save:
            super().save(update_fields=["total_amount", "updated_at"])
        return total


class QuotationItem(TimeStampedModel):
    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name="items")
    asset_type = models.ForeignKey(
        "assets.AssetType", on_delete=models.SET_NULL, null=True, blank=True, related_name="quotation_items"
    )
    device_model = models.ForeignKey(
        "assets.DeviceModel", on_delete=models.SET_NULL, null=True, blank=True, related_name="quotation_items"
    )
    material_type = models.ForeignKey(
        "assets.MaterialType", on_delete=models.SET_NULL, null=True, blank=True, related_name="quotation_items"
    )
    description = models.CharField(max_length=300)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.description} x{self.quantity}"

    @property
    def line_total(self) -> Decimal:
        return (self.unit_price or Decimal("0")) * self.quantity
