"""
Setup / master-data models.

These back the "Setup Menu" for basic data entry (Company profile, document
numbering, payment terms, terms & conditions templates, warranty presets) so
that reference data is data-driven and editable by admins instead of being
hardcoded in the codebase.
"""

from __future__ import annotations

from datetime import datetime

from django.conf import settings
from django.db import models

from common.models import TimeStampedModel
from common.utils import upload_to_path


class Company(TimeStampedModel):
    """Organisation profile used on printed documents (work orders, invoices)."""

    name = models.CharField(max_length=300)
    legal_name = models.CharField(max_length=300, blank=True)
    logo = models.ImageField(upload_to=upload_to_path, blank=True)
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state_province = models.CharField(max_length=150, blank=True)
    country = models.CharField(max_length=100, default="Pakistan")
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    website = models.URLField(blank=True)
    tax_id = models.CharField(max_length=100, blank=True, help_text="NTN / Tax registration number")
    registration_number = models.CharField(max_length=100, blank=True)
    default_currency = models.CharField(max_length=3, default="PKR")
    is_primary = models.BooleanField(
        default=False, help_text="The primary company shown on documents."
    )

    class Meta:
        verbose_name_plural = "companies"
        ordering = ["-is_primary", "name"]

    def __str__(self):
        return self.name


class NumberingScheme(TimeStampedModel):
    """
    Configurable auto-numbering for every coded entity in the platform
    (assets, suppliers, clients, POs, invoices, work orders, …).

    A code is built as: ``{prefix}{sep}{year?}{sep}{zero-padded number}``,
    e.g. ``WO-2026-00042``. Numbers are handed out atomically by
    :func:`common.codes.generate_code`.
    """

    class Entity(models.TextChoices):
        ASSET = "asset", "Asset / Device"
        SUPPLIER = "supplier", "Supplier"
        CLIENT = "client", "Client"
        PURCHASE_ORDER = "purchase_order", "Purchase Order"
        INVOICE = "invoice", "Invoice"
        WORK_ORDER = "work_order", "Work Order"
        PROJECT = "project", "Project"
        TICKET = "ticket", "Ticket"
        GOODS_RECEIPT = "goods_receipt", "Goods Receipt"
        ISSUANCE = "issuance", "Inventory Issuance"
        INVENTORY_ITEM = "inventory_item", "Inventory Item (SKU)"

    entity = models.CharField(max_length=30, choices=Entity.choices, unique=True)
    prefix = models.CharField(max_length=12)
    separator = models.CharField(max_length=3, default="-")
    include_year = models.BooleanField(default=True)
    padding = models.PositiveSmallIntegerField(default=5, help_text="Zero-pad width for the sequence number")
    next_number = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["entity"]

    def __str__(self):
        return f"{self.get_entity_display()} ({self.prefix})"

    def build(self, number: int) -> str:
        segments = [self.prefix]
        if self.include_year:
            segments.append(str(datetime.now().year))
        segments.append(str(number).zfill(self.padding))
        return self.separator.join(s for s in segments if s)

    def preview(self) -> str:
        return self.build(self.next_number)


class PaymentTerms(TimeStampedModel):
    """Reusable payment terms (e.g. Net 30) referenced by work orders / POs."""

    name = models.CharField(max_length=120, unique=True)
    code = models.CharField(max_length=30, blank=True, db_index=True)
    days = models.PositiveSmallIntegerField(default=0, help_text="Net days until payment is due")
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "payment terms"
        ordering = ["days", "name"]

    def __str__(self):
        return self.name


class TermsTemplate(TimeStampedModel):
    """Terms & conditions / safety instruction templates for printed documents."""

    class Category(models.TextChoices):
        WORK_ORDER = "work_order", "Work Order"
        SAFETY = "safety", "Safety Instructions"
        GENERAL = "general", "General"

    name = models.CharField(max_length=200)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.GENERAL)
    body = models.TextField()
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["category", "name"]

    def __str__(self):
        return f"{self.name} ({self.get_category_display()})"


class WarrantyPeriodPreset(TimeStampedModel):
    """Common warranty durations selectable when registering assets/warranties."""

    label = models.CharField(max_length=100, unique=True, help_text="e.g. '1 Year', '18 Months'")
    months = models.PositiveSmallIntegerField(help_text="Duration in months")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["months"]

    def __str__(self):
        return self.label


class EscalationPolicy(TimeStampedModel):
    """Data-driven ticket escalation ladder (client hierarchy:
    Group Head > Operations/Marketing Head > Supervisors > Technician).

    One row per trigger. The beat task walks active policies and escalates
    matching tickets once per trigger, notifying ``escalate_to_role`` users
    (and optionally ``also_notify_role`` — e.g. operations always hears about
    assignment breaches).
    """

    class Trigger(models.TextChoices):
        ASSIGNMENT_SLA = "assignment_sla", "Unassigned beyond window"
        RESPONSE_SLA = "response_sla", "No response within SLA"
        DUE_DATE = "due_date", "Past due date"

    trigger = models.CharField(max_length=20, choices=Trigger.choices, unique=True)
    hours = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Window in hours (assignment trigger). Blank = response SLA uses per-priority windows; due date fires the day after.",
    )
    escalate_to_role = models.CharField(
        max_length=20, default="group_head",
        help_text="Role that receives the escalation (accounts.User.Role value)",
    )
    also_notify_role = models.CharField(
        max_length=20, blank=True, default="ops_manager",
        help_text="Additional role notified alongside (blank = none)",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["trigger"]
        verbose_name_plural = "escalation policies"

    def __str__(self):
        return f"{self.get_trigger_display()} -> {self.escalate_to_role}"
