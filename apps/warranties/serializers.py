from rest_framework import serializers

from .models import Warranty


class WarrantySerializer(serializers.ModelSerializer):
    device_code = serializers.CharField(source="device.asset_code", read_only=True, default=None)
    supplier_name = serializers.CharField(source="supplier.name", read_only=True, default=None)
    is_expired = serializers.BooleanField(read_only=True)

    class Meta:
        model = Warranty
        fields = [
            "id", "device", "device_code", "supplier", "supplier_name",
            "warranty_type", "status", "start_date", "end_date",
            "coverage_details", "reference_number", "notes",
            "is_expired", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
