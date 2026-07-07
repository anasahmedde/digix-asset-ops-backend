from django.contrib import admin

from .models import (
    GoodsReceipt,
    InventoryCategory,
    InventoryItem,
    Issuance,
    StockMovement,
)


@admin.register(InventoryCategory)
class InventoryCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)


@admin.register(InventoryItem)
class InventoryItemAdmin(admin.ModelAdmin):
    list_display = ("material_type", "category", "sku", "quantity", "min_stock_level", "location")
    list_filter = ("location", "category")
    search_fields = ("sku", "material_type__name")
    raw_id_fields = ("material_type",)
    readonly_fields = ("sku",)


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = ("item", "movement_type", "quantity", "performed_by", "created_at")
    list_filter = ("movement_type",)
    raw_id_fields = ("item", "performed_by")


@admin.register(GoodsReceipt)
class GoodsReceiptAdmin(admin.ModelAdmin):
    list_display = ("grn_number", "item", "quantity", "work_order", "received_by", "created_at")
    search_fields = ("grn_number", "item__sku")
    raw_id_fields = ("item", "work_order", "received_by")
    readonly_fields = ("grn_number",)


@admin.register(Issuance)
class IssuanceAdmin(admin.ModelAdmin):
    list_display = ("issue_number", "item", "quantity", "issued_to_site", "issued_by", "created_at")
    search_fields = ("issue_number", "item__sku")
    raw_id_fields = ("item", "issued_to_site", "issued_to_work_order", "issued_to_user", "issued_by")
    readonly_fields = ("issue_number",)
