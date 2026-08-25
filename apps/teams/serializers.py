from rest_framework import serializers

from .models import Project, ProjectBottleneck, ProjectMember, ProjectMilestone, ProjectScopeItem


class ProjectBottleneckSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectBottleneck
        fields = [
            "id", "project", "title", "severity", "is_resolved", "resolved_at", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class ProjectMemberSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source="user.get_full_name", read_only=True)

    class Meta:
        model = ProjectMember
        fields = ["id", "project", "user", "user_name", "role", "created_at"]
        read_only_fields = ["id", "created_at"]


class ProjectScopeItemSerializer(serializers.ModelSerializer):
    device_code = serializers.CharField(source="device.asset_code", read_only=True)
    device_name = serializers.CharField(source="device.display_name", read_only=True, default=None)
    component_name = serializers.CharField(source="component.name", read_only=True, default=None)
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)

    class Meta:
        model = ProjectScopeItem
        fields = [
            "id", "project", "device", "device_code", "device_name",
            "component", "component_name", "quantity",
            "site", "site_name", "start_date", "notes", "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def validate(self, attrs):
        component = attrs.get("component")
        device = attrs.get("device") or getattr(self.instance, "device", None)
        if component and device and component.device_id != device.id:
            raise serializers.ValidationError({"component": "Component does not belong to this asset."})
        return attrs


class ProjectMilestoneSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectMilestone
        fields = ["id", "project", "title", "due_date", "completed_at", "order", "created_at"]
        read_only_fields = ["id", "created_at"]


class ProjectListSerializer(serializers.ModelSerializer):
    assets_count = serializers.IntegerField(source="devices.count", read_only=True)
    client_name = serializers.CharField(source="client.name", read_only=True, default=None)
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)
    manager_name = serializers.CharField(source="manager.get_full_name", read_only=True, default=None)
    bottleneck_count = serializers.IntegerField(read_only=True, default=0)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    phase_display = serializers.CharField(source="get_phase_display", read_only=True)

    class Meta:
        model = Project
        fields = [
            "id", "name", "location", "image", "client", "client_name",
            "site", "site_name", "status", "status_display",
            "phase", "phase_display", "progress",
            "start_date", "target_date", "completed_date",
            "manager", "manager_name", "bottleneck_count", "created_at",
         "assets_count",]


class ProjectDetailSerializer(serializers.ModelSerializer):
    client_name = serializers.CharField(source="client.name", read_only=True, default=None)
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)
    manager_name = serializers.CharField(source="manager.get_full_name", read_only=True, default=None)
    bottlenecks = ProjectBottleneckSerializer(many=True, read_only=True)
    members = ProjectMemberSerializer(many=True, read_only=True)
    scope_items = ProjectScopeItemSerializer(many=True, read_only=True)
    milestones = ProjectMilestoneSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    phase_display = serializers.CharField(source="get_phase_display", read_only=True)

    class Meta:
        model = Project
        fields = [
            "id", "name", "description", "location", "image",
            "client", "client_name", "site", "site_name",
            "status", "status_display", "phase", "phase_display",
            "progress", "start_date", "target_date", "completed_date",
            "manager", "manager_name", "budget", "notes",
            "bottlenecks", "members", "scope_items", "milestones",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
