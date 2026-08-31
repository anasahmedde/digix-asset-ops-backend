from rest_framework import serializers

from apps.clients.models import Client

from .models import (
    DeviceInstallation,
    HandoverRecord,
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
    step_type_display = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = InstallationStep
        fields = [
            "id", "installation", "step_type", "step_type_display", "custom_label",
            "step_number", "status", "status_display",
            "assigned_team", "description",
            "started_at", "completed_at", "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def get_step_type_display(self, obj):
        return obj.custom_label or obj.get_step_type_display()


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
    installed_by_phone = serializers.CharField(source="installed_by.phone", read_only=True, default=None)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True, default=None)
    project_name = serializers.CharField(source="device.project.name", read_only=True, default=None)
    poc_name = serializers.CharField(source="device.assigned_client.contact_person", read_only=True, default=None)
    poc_phone = serializers.CharField(source="device.assigned_client.contact_phone", read_only=True, default=None)
    client_names = serializers.SerializerMethodField()
    progress = serializers.SerializerMethodField()
    client_delays = serializers.SerializerMethodField()
    on_hold_steps = serializers.SerializerMethodField()
    escalated = serializers.SerializerMethodField()

    def get_escalated(self, obj):
        return bool(obj.escalation_state)

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

    def get_on_hold_steps(self, obj):
        return sum(1 for s in obj.steps.all() if s.status == InstallationStep.StepStatus.ON_HOLD)


class DeviceInstallationListSerializer(_InstallationCommonMixin, serializers.ModelSerializer):
    class Meta:
        model = DeviceInstallation
        fields = [
            "id", "device", "device_code", "device_name", "asset_name", "asset_type_name",
            "client_names", "project_name", "poc_name", "poc_phone",
            "site", "site_name", "installed_by", "installed_by_name", "installed_by_phone",
            "vendor", "vendor_name",
            "installed_at", "removed_at", "due_date", "completed_at",
            "escalated", "escalation_state",
            "progress", "client_delays", "on_hold_steps", "created_at",
        ]
        read_only_fields = ["id", "completed_at", "escalation_state", "created_at"]


class HandoverRecordSerializer(serializers.ModelSerializer):
    client_name = serializers.CharField(source="client.name", read_only=True)
    site_name = serializers.CharField(source="site.name", read_only=True)
    performed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = HandoverRecord
        fields = [
            "id", "handover_date", "accepted_by_name", "acceptance_notes", "signature",
            "client", "client_name", "site", "site_name", "performed_by_name", "created_at",
        ]
        read_only_fields = fields

    def get_performed_by_name(self, obj):
        return obj.performed_by.get_full_name() or obj.performed_by.username if obj.performed_by else None


class HandoverCreateSerializer(serializers.Serializer):
    """Write payload for the installation handover action (multipart)."""

    accepted_by_name = serializers.CharField(max_length=200)
    acceptance_notes = serializers.CharField(required=False, allow_blank=True, default="")
    handover_date = serializers.DateField(required=False)
    client = serializers.PrimaryKeyRelatedField(
        queryset=Client.objects.filter(is_active=True), required=False, allow_null=True
    )
    signature = serializers.ImageField(required=False, allow_null=True)


class DeviceInstallationDetailSerializer(_InstallationCommonMixin, serializers.ModelSerializer):
    handover = HandoverRecordSerializer(read_only=True)
    photos = InstallationPhotoSerializer(many=True, read_only=True)
    steps = InstallationStepSerializer(many=True, read_only=True)
    delays = InstallationDelaySerializer(many=True, read_only=True)
    # Optional custom pipeline chosen at creation: entries are either known
    # step-type values or free-text names (stored as 'other' + custom_label).
    # When omitted the default six-step pipeline is seeded.
    step_types = serializers.ListField(
        child=serializers.CharField(max_length=200),
        write_only=True,
        required=False,
        allow_empty=False,
    )
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
            "installed_by", "installed_by_name", "installed_by_phone", "installed_at", "removed_at",
            "vendor", "vendor_name",
            "due_date", "completed_at",
            "escalated", "escalation_state",
            "position_label", "notes", "photos", "steps", "delays", "step_types",
            "handover", "progress", "client_delays", "on_hold_steps", "created_at",
        ]
        read_only_fields = ["id", "completed_at", "escalation_state", "created_at"]

    def create(self, validated_data):
        step_types = validated_data.pop("step_types", None)
        if not step_types:
            return super().create(validated_data)
        # Custom pipeline: suppress the default seed, create the chosen steps.
        # Unknown names become 'other' steps carrying their text as the label.
        seen = []
        for raw in step_types:
            entry = raw.strip()
            if not entry:
                continue
            if entry in InstallationStep.StepType.values:
                spec = (entry, "")
            else:
                spec = (InstallationStep.StepType.OTHER, entry)
            if spec not in seen:
                seen.append(spec)
        if not seen:
            raise serializers.ValidationError({"step_types": "At least one valid step is required."})
        installation = DeviceInstallation(**validated_data)
        installation._skip_default_steps = True
        installation.save()
        InstallationStep.objects.bulk_create(
            [
                InstallationStep(
                    installation=installation, step_type=st, custom_label=label, step_number=i + 1
                )
                for i, (st, label) in enumerate(seen)
            ]
        )
        return installation

    def update(self, instance, validated_data):
        validated_data.pop("step_types", None)
        return super().update(instance, validated_data)
