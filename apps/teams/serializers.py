from rest_framework import serializers

from .models import (
    ProjectCostLine,
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
        project = attrs.get("project") or getattr(self.instance, "project", None)
        if component and device and component.device_id != device.id:
            raise serializers.ValidationError({"component": "Component does not belong to this asset."})
        if self.instance is None and device is not None and project is not None:
            # Every asset has its own ID: it sits on one project, once.
            if ProjectScopeItem.objects.filter(project=project, device=device).exists():
                raise serializers.ValidationError(
                    {"device": f"{device.asset_code} is already in this project's scope."}
                )
            elsewhere = (
                ProjectScopeItem.objects.filter(device=device).exclude(project=project)
                .select_related("project").first()
            )
            other = elsewhere.project if elsewhere else (
                device.project if device.project_id and device.project_id != project.id else None
            )
            if other is not None:
                raise serializers.ValidationError(
                    {"device": f"{device.asset_code} already belongs to project '{other.name}'."}
                )
        return attrs


class ProjectMilestoneSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectMilestone
        fields = ["id", "project", "title", "due_date", "completed_at", "order", "created_at"]
        read_only_fields = ["id", "created_at"]


class _PhaseFollowsTheWorkMixin:
    """Brings the stored phase back in step before the project is rendered.

    Done here rather than with a read-only field because the phase still has
    to be writable: On Hold and Order Lost are set by hand, and a method field
    would silently drop them. Syncing mutates the instance, so the ordinary
    model fields then render the corrected value.
    """

    def to_representation(self, instance):
        instance.sync_phase()
        return super().to_representation(instance)


class ProjectListSerializer(_PhaseFollowsTheWorkMixin, serializers.ModelSerializer):
    assets_count = serializers.IntegerField(source="devices.count", read_only=True)
    client_name = serializers.CharField(source="client.name", read_only=True, default=None)
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)
    manager_name = serializers.CharField(source="manager.get_full_name", read_only=True, default=None)
    bottleneck_count = serializers.IntegerField(read_only=True, default=0)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    phase_display = serializers.CharField(source="get_phase_display", read_only=True)
    contract_type_display = serializers.CharField(source="get_contract_type_display", read_only=True)
    progress = serializers.SerializerMethodField()

    # Item 4: a project covers several sites; the list names them.
    site_names = serializers.SerializerMethodField()

    def get_site_names(self, obj):
        return [site.name for site in obj.sites.all()]

    class Meta:
        model = Project
        fields = [
            "id", "name", "location", "image", "client", "client_name",
            "site", "site_name", "status", "status_display",
            "phase", "phase_display", "progress",
            "contract_type", "contract_type_display", "rental_end_date",
            "start_date", "target_date", "completed_date",
            "manager", "manager_name", "bottleneck_count", "created_at",
            "assets_count", "sites", "site_names",
        ]

    def get_progress(self, obj):
        return obj.computed_progress()


class ProjectDetailSerializer(_PhaseFollowsTheWorkMixin, serializers.ModelSerializer):
    client_name = serializers.CharField(source="client.name", read_only=True, default=None)
    # Who the work is for, with the contact the team will actually ring.
    client_contact_person = serializers.CharField(
        source="client.contact_person", read_only=True, default=None
    )
    client_contact_phone = serializers.CharField(
        source="client.contact_phone", read_only=True, default=None
    )
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)
    manager_name = serializers.CharField(source="manager.get_full_name", read_only=True, default=None)
    bottlenecks = ProjectBottleneckSerializer(many=True, read_only=True)
    members = ProjectMemberSerializer(many=True, read_only=True)
    scope_items = ProjectScopeItemSerializer(many=True, read_only=True)
    milestones = ProjectMilestoneSerializer(many=True, read_only=True)
    site_names = serializers.SerializerMethodField()

    def get_site_names(self, obj):
        return [site.name for site in obj.sites.all()]
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    phase_display = serializers.CharField(source="get_phase_display", read_only=True)
    contract_type_display = serializers.CharField(source="get_contract_type_display", read_only=True)
    progress = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = [
            "id", "name", "description", "location", "image",
            "client", "client_name", "client_contact_person", "client_contact_phone",
            "site", "site_name",
            "status", "status_display", "phase", "phase_display",
            "contract_type", "contract_type_display", "rental_end_date",
            "progress", "start_date", "target_date", "completed_date",
            "manager", "manager_name", "budget", "notes", "sites", "site_names",
            "bottlenecks", "members", "scope_items", "milestones",
            "phase_progress", "created_at", "updated_at",
        ]
        # The budget is what planning arrives at and approval freezes — not a
        # number typed when the project is opened.
        read_only_fields = ["id", "budget", "created_at", "updated_at"]

    # How far each phase of the work has got, counted from the work itself.
    phase_progress = serializers.SerializerMethodField()

    def get_progress(self, obj):
        return obj.computed_progress()

    def get_phase_progress(self, obj):
        from .phases import phase_progress

        return phase_progress(obj)


class ProjectCostLineSerializer(serializers.ModelSerializer):
    """An overhead line on a project's cost plan."""

    amount = serializers.SerializerMethodField()
    actual_amount = serializers.SerializerMethodField()

    class Meta:
        model = ProjectCostLine
        fields = [
            "id", "project", "cost_type", "description", "quantity", "unit_cost",
            "amount", "actual_quantity", "actual_unit_cost", "actual_amount", "created_at",
        ]
        read_only_fields = ["id", "created_at"]
        # A cost nobody planned for carries no planned rate, so the rate is
        # required by validate() only while the budget is still being planned.
        extra_kwargs = {"unit_cost": {"required": False}, "cost_type": {"required": False}}

    def get_amount(self, obj):
        return str(obj.amount)

    def get_actual_amount(self, obj):
        return None if obj.actual_amount is None else str(obj.actual_amount)

    def validate_cost_type(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Say what kind of cost this is.")
        return value[:100]

    def validate(self, attrs):
        # An overhead is known by what it is for; the type is that same wording.
        if self.instance is None and not (attrs.get("cost_type") or "").strip():
            attrs["cost_type"] = (attrs.get("description") or "Overhead").strip()[:100]
        project = attrs.get("project") or getattr(self.instance, "project", None)
        plan = getattr(project, "cost_plan", None) if project else None
        locked = plan is not None and not plan.is_editable
        if not locked:
            if self.instance is None and attrs.get("unit_cost") is None:
                raise serializers.ValidationError({"unit_cost": "Give the rate for this cost."})
            return attrs

        # The approved estimate does not move. What things actually cost does,
        # so the actual columns stay open all through execution.
        if self.instance is None:
            # A cost nobody planned for belongs to the actuals, not to the
            # figure that was signed off.
            attrs["quantity"] = 0
            attrs["unit_cost"] = 0
        elif any(field in attrs for field in ("quantity", "unit_cost")):
            raise serializers.ValidationError(
                f"The budget is {plan.get_status_display().lower()} — revise it to change the "
                f"planned figures. Recording what it actually cost is always allowed."
            )
        return attrs
