from rest_framework import serializers

from .models import AttendanceRecord


class AttendanceRecordSerializer(serializers.ModelSerializer):
    user_name = serializers.SerializerMethodField()
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)
    check_type_display = serializers.CharField(source="get_check_type_display", read_only=True)

    class Meta:
        model = AttendanceRecord
        fields = [
            "id", "user", "user_name", "check_type", "check_type_display",
            "latitude", "longitude", "accuracy", "site", "site_name",
            "note", "created_at",
        ]
        read_only_fields = ["id", "user", "created_at"]

    def get_user_name(self, obj):
        return (obj.user.get_full_name() or "").strip() or obj.user.username
