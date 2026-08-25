from rest_framework import serializers

from .models import Warranty


class WarrantySerializer(serializers.ModelSerializer):
    device_code = serializers.CharField(source="device.asset_code", read_only=True, default=None)
    device_name = serializers.CharField(source="device.display_name", read_only=True, default=None)
    component_name = serializers.CharField(source="component.name", read_only=True, default=None)
    supplier_name = serializers.CharField(source="supplier.name", read_only=True, default=None)
    is_expired = serializers.BooleanField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    warranty_type_display = serializers.CharField(source="get_warranty_type_display", read_only=True)

    class Meta:
        model = Warranty
        fields = [
            "id", "device", "device_code", "device_name", "component", "component_name",
            "supplier", "supplier_name",
            "warranty_type", "warranty_type_display", "status", "status_display",
            "start_date", "end_date", "months", "reissued_from",
            "coverage_details", "reference_number", "notes",
            "is_expired", "created_at", "updated_at",
        ]
        # reissued_from is set exclusively by the /reissue/ action.
        read_only_fields = ["id", "reissued_from", "created_at", "updated_at"]

    def validate(self, attrs):
        component = attrs.get("component")
        device = attrs.get("device") or getattr(self.instance, "device", None)
        if component and device and component.device_id != device.id:
            raise serializers.ValidationError({"component": "Component does not belong to this device."})
        return attrs

    def update(self, instance, validated_data):
        # A warranty is bound to its device/component for life; ignore reassignment.
        validated_data.pop("device", None)
        validated_data.pop("component", None)
        return super().update(instance, validated_data)
