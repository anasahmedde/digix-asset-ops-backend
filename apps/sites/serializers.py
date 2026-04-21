from rest_framework import serializers

from .models import DeviceInstallation, InstallationPhoto, Site, SiteZone


class SiteZoneSerializer(serializers.ModelSerializer):
    class Meta:
        model = SiteZone
        fields = ["id", "site", "name", "description", "floor", "created_at"]
        read_only_fields = ["id", "created_at"]


class SiteListSerializer(serializers.ModelSerializer):
    device_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Site
        fields = [
            "id", "name", "address", "city", "country", "latitude", "longitude",
            "client", "is_active", "device_count", "created_at",
        ]


class SiteDetailSerializer(serializers.ModelSerializer):
    zones = SiteZoneSerializer(many=True, read_only=True)

    class Meta:
        model = Site
        fields = [
            "id", "name", "address", "city", "country", "latitude", "longitude",
            "contact_person", "contact_phone", "contact_email",
            "access_instructions", "operating_hours", "client",
            "floor_plan", "is_active", "zones", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class InstallationPhotoSerializer(serializers.ModelSerializer):
    class Meta:
        model = InstallationPhoto
        fields = [
            "id", "installation", "photo_type", "image", "caption",
            "latitude", "longitude", "taken_by", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class DeviceInstallationSerializer(serializers.ModelSerializer):
    photos = InstallationPhotoSerializer(many=True, read_only=True)
    device_code = serializers.CharField(source="device.asset_code", read_only=True)
    site_name = serializers.CharField(source="site.name", read_only=True)

    class Meta:
        model = DeviceInstallation
        fields = [
            "id", "device", "device_code", "site", "site_name", "zone",
            "installed_by", "installed_at", "removed_at",
            "position_label", "notes", "photos", "created_at",
        ]
        read_only_fields = ["id", "created_at"]
