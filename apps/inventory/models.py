from django.conf import settings
from django.db import models

from common.models import TimeStampedModel


class InventoryItem(TimeStampedModel):
    class Location(models.TextChoices):
        WAREHOUSE = "warehouse", "Warehouse"
        SITE = "site", "On Site"
        IN_TRANSIT = "in_transit", "In Transit"

    material_type = models.ForeignKey(
        "assets.MaterialType", on_delete=models.PROTECT, related_name="inventory_items"
    )
    sku = models.CharField(max_length=100, unique=True)
    quantity = models.IntegerField(default=0)
    min_stock_level = models.IntegerField(default=5)
    location = models.CharField(max_length=15, choices=Location.choices, default=Location.WAREHOUSE)
    site = models.ForeignKey(
        "sites.Site", on_delete=models.SET_NULL, null=True, blank=True, related_name="inventory_items"
    )
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["material_type__name"]

    def __str__(self):
        return f"{self.material_type.name} ({self.sku})"

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
