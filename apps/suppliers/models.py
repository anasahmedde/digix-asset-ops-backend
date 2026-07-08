from django.db import models

from common.codes import generate_code
from common.models import TimeStampedModel


class SupplierServiceCategory(TimeStampedModel):
    """Category of service a supplier provides (Installation, Fabrication, Supply, …)."""

    name = models.CharField(max_length=150, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "supplier service categories"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Supplier(TimeStampedModel):
    name = models.CharField(max_length=300)
    code = models.CharField(max_length=50, unique=True, blank=True, db_index=True)
    service_categories = models.ManyToManyField(
        SupplierServiceCategory, blank=True, related_name="suppliers"
    )
    # Legacy primary contact (kept for backward compatibility). Additional
    # contacts live in SupplierContact.
    contact_person = models.CharField(max_length=200, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    website = models.URLField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = generate_code("supplier", model=type(self), field="code")
        super().save(*args, **kwargs)


class SupplierContact(TimeStampedModel):
    """One of potentially many named contacts for a supplier."""

    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name="contacts")
    name = models.CharField(max_length=200)
    designation = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    is_primary = models.BooleanField(default=False)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-is_primary", "name"]

    def __str__(self):
        return f"{self.name} ({self.supplier.name})"
