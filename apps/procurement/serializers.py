from rest_framework import serializers

from .models import PurchaseOrder, PurchaseOrderItem


class PurchaseOrderItemSerializer(serializers.ModelSerializer):
    line_total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = PurchaseOrderItem
        fields = [
            "id", "purchase_order", "description",
            "quantity", "unit_price", "received_quantity",
            "line_total",
        ]
        read_only_fields = ["id"]


class PurchaseOrderSerializer(serializers.ModelSerializer):
    supplier_name = serializers.CharField(source="supplier.name", read_only=True, default=None)
    ordered_by_name = serializers.CharField(source="ordered_by.get_full_name", read_only=True, default=None)
    items = PurchaseOrderItemSerializer(many=True, read_only=True)

    class Meta:
        model = PurchaseOrder
        fields = [
            "id", "po_number", "supplier", "supplier_name",
            "status", "currency", "order_date", "expected_delivery",
            "total_amount", "notes",
            "ordered_by", "ordered_by_name", "approved_by",
            "items", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
