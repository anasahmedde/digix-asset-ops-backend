from rest_framework import serializers

from .models import Document


class DocumentSerializer(serializers.ModelSerializer):
    uploaded_by_name = serializers.CharField(source="uploaded_by.get_full_name", read_only=True, default=None)
    device_code = serializers.CharField(source="device.asset_code", read_only=True, default=None)
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)
    project_name = serializers.CharField(source="project.name", read_only=True, default=None)

    class Meta:
        model = Document
        fields = [
            "id", "title", "doc_type", "file", "file_size", "description",
            "device", "device_code", "site", "site_name",
            "project", "project_name", "installation",
            "uploaded_by", "uploaded_by_name", "created_at",
        ]
        read_only_fields = ["id", "created_at"]
