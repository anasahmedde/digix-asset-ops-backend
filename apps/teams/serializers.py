from rest_framework import serializers

from .models import (
    BOMAllocation,
    Project,
    ProjectBOMLine,
    ProjectBottleneck,
    ProjectMember,
    ProjectMilestone,
    ProjectScopeItem,
)


class BOMAllocationSerializer(serializers.ModelSerializer):
    device_code = serializers.CharField(source="device.asset_code", read_only=True, default=None)
    device_serial = serializers.CharField(source="device.serial_number", read_only=True, default=None)
    item_name = serializers.CharField(source="inventory_item.material_type.name", read_only=True, default=None)
    allocated_by_name = serializers.CharField(source="allocated_by.get_full_name", read_only=True, default=None)

    class Meta:
        model = BOMAllocation
        fields = [
            "id", "bom_line", "device", "device_code", "device_serial",
            "inventory_item", "item_name", "quantity", "status",
            "allocated_by", "allocated_by_name", "created_at",
        ]
        read_only_fields = ["id", "status", "allocated_by", "created_at"]


class ProjectBOMLineSerializer(serializers.ModelSerializer):
    asset_type_name = serializers.CharField(source="asset_type.name", read_only=True, default=None)
    device_model_name = serializers.CharField(source="device_model.name", read_only=True, default=None)
    material_type_name = serializers.CharField(source="material_type.name", read_only=True, default=None)
    allocated_quantity = serializers.IntegerField(read_only=True)
    issued_quantity = serializers.IntegerField(read_only=True)
    shortage = serializers.IntegerField(read_only=True)
    allocations = BOMAllocationSerializer(many=True, read_only=True)

    class Meta:
        model = ProjectBOMLine
        fields = [
            "id", "project", "asset_type", "asset_type_name",
            "device_model", "device_model_name", "material_type", "material_type_name",
            "description", "quantity", "unit_price",
            "allocated_quantity", "issued_quantity", "shortage",
            "allocations", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Quantity must be a positive integer.")
        return value


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
    contract_type_display = serializers.CharField(source="get_contract_type_display", read_only=True)
    progress = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = [
            "id", "name", "location", "image", "client", "client_name",
            "site", "site_name", "status", "status_display",
            "phase", "phase_display", "progress",
            "contract_type", "contract_type_display", "rental_end_date",
            "start_date", "target_date", "completed_date",
            "manager", "manager_name", "bottleneck_count", "created_at",
         "assets_count",]

    def get_progress(self, obj):
        return obj.computed_progress()


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
    contract_type_display = serializers.CharField(source="get_contract_type_display", read_only=True)
    progress = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = [
            "id", "name", "description", "location", "image",
            "client", "client_name", "site", "site_name",
            "status", "status_display", "phase", "phase_display",
            "contract_type", "contract_type_display", "rental_end_date",
            "progress", "start_date", "target_date", "completed_date",
            "manager", "manager_name", "budget", "notes",
            "bottlenecks", "members", "scope_items", "milestones",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_progress(self, obj):
        return obj.computed_progress()
