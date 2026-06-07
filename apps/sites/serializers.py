from rest_framework import serializers

from .models import DeviceInstallation, InstallationPhoto, InstallationStep, Site, SiteZone


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
            "id", "name", "address", "city", "state_province", "country",
            "latitude", "longitude", "client", "is_active", "device_count", "created_at",
        ]


class SiteDetailSerializer(serializers.ModelSerializer):
    zones = SiteZoneSerializer(many=True, read_only=True)

    class Meta:
        model = Site
        fields = [
            "id", "name", "address", "city", "state_province", "country",
            "latitude", "longitude",
            "contact_person", "contact_phone", "contact_email",
            "access_instructions", "operating_hours", "client",
            "floor_plan", "is_active", "zones", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class InstallationStepSerializer(serializers.ModelSerializer):
    step_type_display = serializers.CharField(source="get_step_type_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = InstallationStep
        fields = [
            "id", "installation", "step_type", "step_type_display",
            "step_number", "status", "status_display",
            "assigned_team", "description",
            "started_at", "completed_at", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class InstallationPhotoSerializer(serializers.ModelSerializer):
    class Meta:
        model = InstallationPhoto
        fields = [
            "id", "installation", "photo_type", "image", "caption",
            "latitude", "longitude", "taken_by", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class DeviceInstallationListSerializer(serializers.ModelSerializer):
    device_code = serializers.CharField(source="device.asset_code", read_only=True)
    site_name = serializers.CharField(source="site.name", read_only=True)
    progress = serializers.SerializerMethodField()

    class Meta:
        model = DeviceInstallation
        fields = [
            "id", "device", "device_code", "site", "site_name",
            "installed_at", "removed_at", "progress", "created_at",
        ]

    def get_progress(self, obj):
        steps = obj.steps.all()
        if not steps:
            return 0
        completed = steps.filter(status="completed").count()
        return round((completed / steps.count()) * 100)


class DeviceInstallationDetailSerializer(serializers.ModelSerializer):
    photos = InstallationPhotoSerializer(many=True, read_only=True)
    steps = InstallationStepSerializer(many=True, read_only=True)
    device_code = serializers.CharField(source="device.asset_code", read_only=True)
    device_name = serializers.CharField(source="device.device_model.__str__", read_only=True)
    device_image = serializers.ImageField(source="device.image", read_only=True)
    device_status = serializers.CharField(source="device.status", read_only=True)
    site_name = serializers.CharField(source="site.name", read_only=True)
    site_city = serializers.CharField(source="site.city", read_only=True)
    progress = serializers.SerializerMethodField()

    class Meta:
        model = DeviceInstallation
        fields = [
            "id", "device", "device_code", "device_name", "device_image", "device_status",
            "site", "site_name", "site_city", "zone",
            "installed_by", "installed_at", "removed_at",
            "position_label", "notes", "photos", "steps", "progress", "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def get_progress(self, obj):
        steps = obj.steps.all()
        if not steps:
            return 0
        completed = steps.filter(status="completed").count()
        return round((completed / steps.count()) * 100)
