from rest_framework import serializers

from .models import (
    DeviceInstallation,
    InstallationDelay,
    InstallationPhoto,
    InstallationStep,
    Site,
    SiteContact,
    SiteZone,
)


class SiteZoneSerializer(serializers.ModelSerializer):
    class Meta:
        model = SiteZone
        fields = ["id", "site", "name", "description", "floor", "created_at"]
        read_only_fields = ["id", "created_at"]


class SiteContactSerializer(serializers.ModelSerializer):
    class Meta:
        model = SiteContact
        fields = [
            "id", "site", "name", "designation", "phone", "email",
            "is_primary", "notes", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class SiteListSerializer(serializers.ModelSerializer):
    device_count = serializers.IntegerField(read_only=True, default=0)
    client_name = serializers.CharField(source="client.name", read_only=True, default=None)

    class Meta:
        model = Site
        fields = [
            "id", "name", "address", "city", "state_province", "country",
            "latitude", "longitude", "client", "client_name",
            "is_active", "device_count", "created_at",
        ]


class SiteDetailSerializer(serializers.ModelSerializer):
    zones = SiteZoneSerializer(many=True, read_only=True)
    contacts = SiteContactSerializer(many=True, read_only=True)
    client_name = serializers.CharField(source="client.name", read_only=True, default=None)

    class Meta:
        model = Site
        fields = [
            "id", "name", "address", "city", "state_province", "country",
            "latitude", "longitude",
            "contact_person", "contact_phone", "contact_email",
            "access_instructions", "operating_hours", "client", "client_name",
            "floor_plan", "is_active", "zones", "contacts", "created_at", "updated_at",
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


class InstallationDelaySerializer(serializers.ModelSerializer):
    cause_display = serializers.CharField(source="get_cause_display", read_only=True)
    step_type = serializers.CharField(source="step.step_type", read_only=True, default=None)
    step_type_display = serializers.CharField(source="step.get_step_type_display", read_only=True, default=None)
    reported_by_name = serializers.CharField(source="reported_by.get_full_name", read_only=True, default=None)

    class Meta:
        model = InstallationDelay
        fields = [
            "id", "installation", "step", "step_type", "step_type_display",
            "cause", "cause_display", "description",
            "reported_by", "reported_by_name", "resolved_at", "created_at",
        ]
        read_only_fields = ["id", "reported_by", "created_at"]

    def validate(self, attrs):
        step = attrs.get("step")
        installation = attrs.get("installation") or getattr(self.instance, "installation", None)
        if step and installation and step.installation_id != installation.id:
            raise serializers.ValidationError({"step": "Step does not belong to this installation."})
        return attrs


class _InstallationCommonMixin(serializers.Serializer):
    """Shared derived fields for the installation tracker."""

    device_code = serializers.CharField(source="device.asset_code", read_only=True)
    device_name = serializers.CharField(source="device.device_model.__str__", read_only=True)
    asset_name = serializers.CharField(source="device.display_name", read_only=True, default=None)
    asset_type_name = serializers.CharField(source="device.asset_type.name", read_only=True, default=None)
    site_name = serializers.CharField(source="site.name", read_only=True)
    installed_by_name = serializers.CharField(source="installed_by.get_full_name", read_only=True, default=None)
    project_name = serializers.CharField(source="device.project.name", read_only=True, default=None)
    poc_name = serializers.CharField(source="device.assigned_client.contact_person", read_only=True, default=None)
    poc_phone = serializers.CharField(source="device.assigned_client.contact_phone", read_only=True, default=None)
    client_names = serializers.SerializerMethodField()
    progress = serializers.SerializerMethodField()
    client_delays = serializers.SerializerMethodField()

    def get_client_names(self, obj):
        names = []
        if obj.device.assigned_client:
            names.append(obj.device.assigned_client.name)
        for client in obj.device.clients.all():
            if client.name not in names:
                names.append(client.name)
        return names

    def get_progress(self, obj):
        steps = obj.steps.all()
        if not steps:
            return 0
        completed = steps.filter(status="completed").count()
        return round((completed / steps.count()) * 100)

    def get_client_delays(self, obj):
        return sum(1 for d in obj.delays.all() if d.cause == InstallationDelay.Cause.CLIENT)


class DeviceInstallationListSerializer(_InstallationCommonMixin, serializers.ModelSerializer):
    class Meta:
        model = DeviceInstallation
        fields = [
            "id", "device", "device_code", "device_name", "asset_name", "asset_type_name",
            "client_names", "project_name", "poc_name", "poc_phone",
            "site", "site_name", "installed_by", "installed_by_name",
            "installed_at", "removed_at", "due_date", "completed_at",
            "progress", "client_delays", "created_at",
        ]
        read_only_fields = ["id", "completed_at", "created_at"]


class DeviceInstallationDetailSerializer(_InstallationCommonMixin, serializers.ModelSerializer):
    photos = InstallationPhotoSerializer(many=True, read_only=True)
    steps = InstallationStepSerializer(many=True, read_only=True)
    delays = InstallationDelaySerializer(many=True, read_only=True)
    device_image = serializers.ImageField(source="device.image", read_only=True)
    device_status = serializers.CharField(source="device.status", read_only=True)
    site_city = serializers.CharField(source="site.city", read_only=True)

    class Meta:
        model = DeviceInstallation
        fields = [
            "id", "device", "device_code", "device_name", "asset_name", "asset_type_name",
            "device_image", "device_status",
            "client_names", "project_name", "poc_name", "poc_phone",
            "site", "site_name", "site_city", "zone",
            "installed_by", "installed_by_name", "installed_at", "removed_at",
            "due_date", "completed_at",
            "position_label", "notes", "photos", "steps", "delays",
            "progress", "client_delays", "created_at",
        ]
        read_only_fields = ["id", "completed_at", "created_at"]
