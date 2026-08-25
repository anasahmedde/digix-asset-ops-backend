from rest_framework import serializers

from .models import Warranty


class WarrantySerializer(serializers.ModelSerializer):
    device_code = serializers.CharField(source="device.asset_code", read_only=True, default=None)
    device_name = serializers.CharField(source="device.display_name", read_only=True, default=None)
    component_name = serializers.CharField(source="component.name", read_only=True, default=None)
    supplier_name = serializers.CharField(source="supplier.name", read_only=True, default=None)
    is_expired = serializers.BooleanField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    # Optional on create — validate() anchors them (procurement date for
    # supplier-side, handover/today for client) and derives end from months.
    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False)
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

        if self.instance is None:
            # Anchoring defaults: supplier-side warranties start at the
            # procurement/delivery date, client warranties at handover (today
            # until the installation handover re-anchors them).
            from dateutil.relativedelta import relativedelta
            from django.utils import timezone

            warranty_type = attrs.get("warranty_type") or Warranty.WarrantyType.MANUFACTURER
            if not attrs.get("start_date"):
                if warranty_type == Warranty.WarrantyType.CLIENT:
                    attrs["start_date"] = timezone.now().date()
                else:
                    attrs["start_date"] = (device.purchase_date if device else None) or timezone.now().date()
            if not attrs.get("end_date"):
                months = attrs.get("months")
                if not months:
                    raise serializers.ValidationError({"end_date": "Provide an end date or a months term."})
                attrs["end_date"] = attrs["start_date"] + relativedelta(months=months)
        return attrs

    def update(self, instance, validated_data):
        # A warranty is bound to its device/component for life; ignore reassignment.
        validated_data.pop("device", None)
        validated_data.pop("component", None)
        return super().update(instance, validated_data)
