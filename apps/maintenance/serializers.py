from rest_framework import serializers

from .models import MaintenanceRecord, MaintenanceSchedule


class MaintenanceScheduleSerializer(serializers.ModelSerializer):
    device_code = serializers.CharField(source="device.asset_code", read_only=True, default=None)
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)
    assigned_to_name = serializers.CharField(source="assigned_to.get_full_name", read_only=True, default=None)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    effective_status = serializers.CharField(read_only=True)

    class Meta:
        model = MaintenanceSchedule
        fields = [
            "id", "title", "maintenance_type", "frequency",
            "device", "device_code", "site", "site_name",
            "assigned_to", "assigned_to_name",
            "next_due", "instructions", "status", "status_display",
            "effective_status", "is_active",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class MaintenanceRecordSerializer(serializers.ModelSerializer):
    schedule_title = serializers.CharField(source="schedule.title", read_only=True, default=None)
    performed_by_name = serializers.CharField(source="performed_by.get_full_name", read_only=True, default=None)

    class Meta:
        model = MaintenanceRecord
        fields = [
            "id", "schedule", "schedule_title",
            "performed_by", "performed_by_name",
            "performed_at", "status", "notes", "cost",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]
