from rest_framework import serializers

from .models import (
    GoodsReceipt,
    InventoryCategory,
    InventoryItem,
    Issuance,
    StockMovement,
)


class InventoryCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = InventoryCategory
        fields = ["id", "name", "description", "is_active", "created_at"]
        read_only_fields = ["id", "created_at"]


class InventoryItemSerializer(serializers.ModelSerializer):
    material_name = serializers.CharField(source="material_type.name", read_only=True, default=None)
    category_name = serializers.CharField(source="category.name", read_only=True, default=None)
    unit = serializers.CharField(source="material_type.unit", read_only=True, default=None)
    is_low_stock = serializers.BooleanField(read_only=True)

    class Meta:
        model = InventoryItem
        fields = [
            "id", "material_type", "material_name", "category", "category_name",
            "sku", "quantity", "min_stock_level", "unit", "location",
            "unit_cost", "notes", "is_low_stock", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "sku", "created_at", "updated_at"]


class StockMovementSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.material_type.name", read_only=True, default=None)
    performed_by_name = serializers.CharField(source="performed_by.get_full_name", read_only=True, default=None)

    class Meta:
        model = StockMovement
        fields = [
            "id", "item", "item_name", "movement_type",
            "quantity", "reference", "notes",
            "performed_by", "performed_by_name", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class GoodsReceiptSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.material_type.name", read_only=True, default=None)
    wo_number = serializers.CharField(source="work_order.wo_number", read_only=True, default=None)
    received_by_name = serializers.CharField(source="received_by.get_full_name", read_only=True, default=None)

    class Meta:
        model = GoodsReceipt
        fields = [
            "id", "grn_number", "work_order", "wo_number", "item", "item_name",
            "quantity", "reference", "received_by", "received_by_name", "notes", "created_at",
        ]
        read_only_fields = ["id", "grn_number", "received_by", "created_at"]


class IssuanceSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.material_type.name", read_only=True, default=None)
    site_name = serializers.CharField(source="issued_to_site.name", read_only=True, default=None)
    issued_by_name = serializers.CharField(source="issued_by.get_full_name", read_only=True, default=None)

    class Meta:
        model = Issuance
        fields = [
            "id", "issue_number", "item", "item_name", "quantity",
            "issued_to_site", "site_name", "issued_to_work_order", "issued_to_user",
            "issued_by", "issued_by_name", "reason", "notes", "created_at",
        ]
        read_only_fields = ["id", "issue_number", "issued_by", "created_at"]

    def validate(self, attrs):
        item = attrs.get("item")
        qty = attrs.get("quantity")
        if item is not None and qty is not None and qty > item.quantity:
            raise serializers.ValidationError(
                {"quantity": f"Only {item.quantity} unit(s) of {item} in stock."}
            )
        return attrs
