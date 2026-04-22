from rest_framework import serializers

from .models import Ticket


class TicketSerializer(serializers.ModelSerializer):
    device_code = serializers.CharField(source="device.asset_code", read_only=True, default=None)
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)
    assigned_to_name = serializers.CharField(source="assigned_to.get_full_name", read_only=True, default=None)
    reported_by_name = serializers.CharField(source="reported_by.get_full_name", read_only=True, default=None)

    class Meta:
        model = Ticket
        fields = [
            "id", "title", "description", "priority", "status", "category",
            "device", "device_code", "site", "site_name",
            "assigned_to", "assigned_to_name", "reported_by", "reported_by_name",
            "due_date", "resolved_at", "resolution_notes",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
