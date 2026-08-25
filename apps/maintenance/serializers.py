from rest_framework import serializers

from .models import MaintenanceRecord, MaintenanceRecordPhoto, MaintenanceSchedule


class MaintenanceScheduleSerializer(serializers.ModelSerializer):
    device_code = serializers.CharField(source="device.asset_code", read_only=True, default=None)
    device_name = serializers.CharField(source="device.display_name", read_only=True, default=None)
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)
    assigned_to_name = serializers.CharField(source="assigned_to.get_full_name", read_only=True, default=None)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    effective_status = serializers.CharField(read_only=True)
    vendor_names = serializers.SerializerMethodField()

    class Meta:
        model = MaintenanceSchedule
        fields = [
            "id", "title", "maintenance_type", "frequency", "priority",
            "device", "device_code", "device_name", "site", "site_name",
            "assigned_to", "assigned_to_name", "vendors", "vendor_names",
            "next_due", "instructions", "required_components",
            "status", "status_display",
            "effective_status", "is_active",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_vendor_names(self, obj):
        return [v.name for v in obj.vendors.all()]

    def validate_required_components(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Must be a list of components.")
        cleaned = []
        for row in value:
            if not isinstance(row, dict) or not str(row.get("name", "")).strip():
                raise serializers.ValidationError("Each component needs a name.")
            try:
                quantity = int(row.get("quantity", 1))
            except (TypeError, ValueError):
                quantity = 1
            cleaned.append({"name": str(row["name"]).strip()[:200], "quantity": max(1, quantity)})
        return cleaned


class MaintenanceRecordPhotoSerializer(serializers.ModelSerializer):
    class Meta:
        model = MaintenanceRecordPhoto
        fields = ["id", "record", "image", "caption", "taken_by", "created_at"]
        read_only_fields = ["id", "taken_by", "created_at"]


class MaintenanceRecordSerializer(serializers.ModelSerializer):
    schedule_title = serializers.CharField(source="schedule.title", read_only=True, default=None)
    performed_by_name = serializers.CharField(source="performed_by.get_full_name", read_only=True, default=None)
    component_names = serializers.SerializerMethodField()
    photos = MaintenanceRecordPhotoSerializer(many=True, read_only=True)

    class Meta:
        model = MaintenanceRecord
        fields = [
            "id", "schedule", "schedule_title",
            "performed_by", "performed_by_name",
            "performed_at", "status", "notes", "cost",
            "components_used", "component_names", "photos",
            "created_at",
        ]
        read_only_fields = ["id", "performed_by", "created_at"]

    def get_component_names(self, obj):
        return [c.name for c in obj.components_used.all()]

    def validate(self, attrs):
        schedule = attrs.get("schedule") or getattr(self.instance, "schedule", None)
        components = attrs.get("components_used") or []
        if schedule and schedule.device_id:
            for component in components:
                if component.device_id != schedule.device_id:
                    raise serializers.ValidationError(
                        {"components_used": "All components must belong to the schedule's asset."}
                    )
        elif components:
            raise serializers.ValidationError(
                {"components_used": "This schedule has no asset — components cannot be attached."}
            )
        return attrs
