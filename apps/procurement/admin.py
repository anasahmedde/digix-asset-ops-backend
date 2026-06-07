from django.contrib import admin

from .models import PurchaseOrder, PurchaseOrderItem


class PurchaseOrderItemInline(admin.TabularInline):
    model = PurchaseOrderItem
    extra = 1


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ("po_number", "supplier", "status", "total_amount", "order_date", "expected_delivery")
    list_filter = ("status",)
    search_fields = ("po_number",)
    raw_id_fields = ("supplier", "ordered_by", "approved_by")
    inlines = [PurchaseOrderItemInline]


@admin.register(PurchaseOrderItem)
class PurchaseOrderItemAdmin(admin.ModelAdmin):
    list_display = ("purchase_order", "description", "quantity", "unit_price")
    raw_id_fields = ("purchase_order",)
