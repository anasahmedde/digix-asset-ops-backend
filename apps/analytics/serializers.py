from rest_framework import serializers

from .models import Alert, SavedReport


class AlertSerializer(serializers.ModelSerializer):
    device_code = serializers.CharField(source="device.asset_code", read_only=True, default=None)
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)
    site_city = serializers.CharField(source="site.city", read_only=True, default=None)

    class Meta:
        model = Alert
        fields = [
            "id", "title", "message", "severity", "category",
            "device", "device_code", "site", "site_name", "site_city",
            "is_read", "is_dismissed", "read_by", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class SavedReportSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source="created_by.get_full_name", read_only=True, default=None)

    class Meta:
        model = SavedReport
        fields = [
            "id", "name", "report_type", "parameters",
            "created_by", "created_by_name",
            "is_scheduled", "schedule_cron", "created_at",
        ]
        read_only_fields = ["id", "created_at"]
