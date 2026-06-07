from django.contrib import admin

from .models import InventoryItem, StockMovement


@admin.register(InventoryItem)
class InventoryItemAdmin(admin.ModelAdmin):
    list_display = ("material_type", "sku", "quantity", "min_stock_level", "location", "site")
    list_filter = ("location",)
    search_fields = ("sku", "material_type__name")
    raw_id_fields = ("material_type", "site")


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = ("item", "movement_type", "quantity", "performed_by", "created_at")
    list_filter = ("movement_type",)
    raw_id_fields = ("item", "performed_by")
