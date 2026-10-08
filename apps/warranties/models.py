from django.conf import settings
from django.db import models

from common.models import TimeStampedModel


class Warranty(TimeStampedModel):
    class WarrantyType(models.TextChoices):
        MANUFACTURER = "manufacturer", "Manufacturer"
        EXTENDED = "extended", "Extended"
        # The vendor who supplied the asset. Called "supplier" in the data for
        # historical reasons; everywhere a person reads it, it is Vendor.
        SUPPLIER = "supplier", "Vendor"
        CLIENT = "client", "Client Warranty"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        # Shown as Expired; a beat
        # task flips active warranties here once end_date passes.
        EXPIRED = "expired", "Expired"
        REISSUED = "reissued", "Reissued"
        CLAIMED = "claimed", "Pending"
        VOID = "void", "Void"

    device = models.ForeignKey(
        "assets.Device", on_delete=models.CASCADE, related_name="warranties"
    )
    # Optional: a warranty can cover one specific component of the asset.
    component = models.ForeignKey(
        "assets.AssetComponent", on_delete=models.SET_NULL, null=True, blank=True, related_name="warranties"
    )
    supplier = models.ForeignKey(
        "suppliers.Supplier", on_delete=models.SET_NULL, null=True, blank=True, related_name="warranties"
    )
    warranty_type = models.CharField(max_length=20, choices=WarrantyType.choices, default=WarrantyType.MANUFACTURER)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    start_date = models.DateField()
    end_date = models.DateField()
    months = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Term in months (client warranties: 3/6/12)"
    )
    reissued_from = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="reissues",
        help_text="Original warranty this one was reissued from",
    )
    coverage_details = models.TextField(blank=True)
    # Ours, handed out on creation from the warranty's own number series:
    # CLW-… for a client warranty, VNW-… for a vendor's, CPW-… for a part's.
    reference_number = models.CharField(max_length=200, unique=True, blank=True)
    # The vendor's certificate or warranty number, as their paperwork states it.
    vendor_reference = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-end_date"]
        verbose_name_plural = "warranties"

    def __str__(self):
        return f"{self.device.asset_code} - {self.warranty_type} ({self.status})"

    @property
    def series(self) -> str:
        """Which number series this warranty draws from."""
        if self.warranty_type == self.WarrantyType.CLIENT:
            return "client_warranty"
        return "component_warranty" if self.component_id else "vendor_warranty"

    def save(self, *args, **kwargs):
        if not self.reference_number:
            from common.codes import generate_code

            self.reference_number = generate_code(self.series, model=type(self), field="reference_number")
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = [*kwargs["update_fields"], "reference_number"]
        super().save(*args, **kwargs)

    @property
    def is_expired(self):
        from django.utils import timezone
        return self.end_date < timezone.now().date()


class WarrantyClaim(TimeStampedModel):
    """A claim made on a vendor's cover: the asset's, or a part's.

    The standard run of a warranty claim: it is raised with the fault and the
    day it failed, sent to the vendor (who gives it their own RMA or claim
    number), the vendor approves or rejects it, and an approved claim is
    settled - repaired, replaced, a credit note or a refund - and closed. A
    claim can be withdrawn until the vendor has decided.
    """

    class Status(models.TextChoices):
        RAISED = "raised", "Raised"
        SUBMITTED = "submitted", "Sent to Vendor"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        CLOSED = "closed", "Closed"
        WITHDRAWN = "withdrawn", "Withdrawn"

    class Resolution(models.TextChoices):
        REPAIRED = "repaired", "Repaired"
        REPLACED = "replaced", "Replaced"
        CREDIT_NOTE = "credit_note", "Credit Note"
        REFUND = "refund", "Refund"

    NEXT = {
        Status.RAISED: (Status.SUBMITTED, Status.WITHDRAWN),
        Status.SUBMITTED: (Status.APPROVED, Status.REJECTED, Status.WITHDRAWN),
        Status.APPROVED: (Status.CLOSED,),
        Status.REJECTED: (),
        Status.CLOSED: (),
        Status.WITHDRAWN: (),
    }

    claim_number = models.CharField(max_length=50, unique=True, blank=True)
    # Exactly one of these: the vendor's cover on an asset, or on a part.
    warranty = models.ForeignKey(
        Warranty, on_delete=models.PROTECT, null=True, blank=True, related_name="warranty_claims"
    )
    inventory_unit = models.ForeignKey(
        "inventory.InventoryUnit", on_delete=models.PROTECT, null=True, blank=True,
        related_name="warranty_claims",
    )
    # The asset concerned: the warranty's, or the one the part is fitted in.
    device = models.ForeignKey(
        "assets.Device", on_delete=models.SET_NULL, null=True, blank=True, related_name="warranty_claims"
    )
    supplier = models.ForeignKey(
        "suppliers.Supplier", on_delete=models.SET_NULL, null=True, blank=True, related_name="warranty_claims"
    )
    fault = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    failure_date = models.DateField()
    expected_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    evidence = models.FileField(upload_to="warranty_claims/", blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.RAISED)
    # The vendor's own RMA or claim number, given when it is sent to them.
    vendor_reference = models.CharField(max_length=200, blank=True)
    resolution = models.CharField(max_length=12, choices=Resolution.choices, blank=True)
    recovered_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    # Every step, with who took it and why: the claim's own history.
    history = models.TextField(blank=True)
    raised_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="warranty_claims",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.claim_number or "Warranty claim"

    def save(self, *args, **kwargs):
        if not self.claim_number:
            from common.codes import generate_code

            self.claim_number = generate_code("warranty_claim", model=type(self), field="claim_number")
        super().save(*args, **kwargs)
