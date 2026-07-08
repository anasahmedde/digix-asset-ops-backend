from django.contrib import admin

from .models import WorkOrder, WorkOrderItem


class WorkOrderItemInline(admin.TabularInline):
    model = WorkOrderItem
    extra = 0


@admin.register(WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    list_display = ["wo_number", "title", "order_type", "status", "supplier", "total_amount", "expected_delivery"]
    list_filter = ["status", "order_type", "currency"]
    search_fields = ["wo_number", "title", "supplier__name"]
    readonly_fields = ["wo_number", "total_amount", "approved_at", "issued_at"]
    inlines = [WorkOrderItemInline]


@admin.register(WorkOrderItem)
class WorkOrderItemAdmin(admin.ModelAdmin):
    list_display = ["work_order", "description", "quantity", "unit_price", "received_quantity"]
    search_fields = ["description", "work_order__wo_number"]
