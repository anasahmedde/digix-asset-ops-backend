from rest_framework import serializers

from .models import InventoryItem, StockMovement


class InventoryItemSerializer(serializers.ModelSerializer):
    material_name = serializers.CharField(source="material_type.name", read_only=True, default=None)
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)
    is_low_stock = serializers.BooleanField(read_only=True)

    class Meta:
        model = InventoryItem
        fields = [
            "id", "material_type", "material_name", "sku",
            "quantity", "min_stock_level", "location",
            "site", "site_name", "unit_cost", "notes",
            "is_low_stock", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class StockMovementSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.material_type.name", read_only=True, default=None)
    performed_by_name = serializers.CharField(source="performed_by.get_full_name", read_only=True, default=None)

    class Meta:
        model = StockMovement
        fields = [
            "id", "item", "item_name", "movement_type",
            "quantity", "reference", "notes",
            "performed_by", "performed_by_name",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]
