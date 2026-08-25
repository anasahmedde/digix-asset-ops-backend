from django.db import models

from common.models import TimeStampedModel


class Warranty(TimeStampedModel):
    class WarrantyType(models.TextChoices):
        MANUFACTURER = "manufacturer", "Manufacturer"
        EXTENDED = "extended", "Extended"
        SUPPLIER = "supplier", "Supplier"
        CLIENT = "client", "Client Warranty"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        # "expired" is surfaced to the client as Warranty Completed; a beat
        # task flips active warranties here once end_date passes.
        EXPIRED = "expired", "Warranty Completed"
        REISSUED = "reissued", "Reissued"
        CLAIMED = "claimed", "Claimed"
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
    reference_number = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-end_date"]
        verbose_name_plural = "warranties"

    def __str__(self):
        return f"{self.device.asset_code} - {self.warranty_type} ({self.status})"

    @property
    def is_expired(self):
        from django.utils import timezone
        return self.end_date < timezone.now().date()
