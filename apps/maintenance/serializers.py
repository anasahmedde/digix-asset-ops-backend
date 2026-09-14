from rest_framework import serializers

from .models import MaintenanceRecord, MaintenanceRecordPhoto, MaintenanceSchedule


class MaintenanceScheduleSerializer(serializers.ModelSerializer):
    device_code = serializers.CharField(source="device.asset_code", read_only=True, default=None)
    device_name = serializers.CharField(source="device.display_name", read_only=True, default=None)
    device_status = serializers.CharField(source="device.status", read_only=True, default=None)
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)
    assigned_to_name = serializers.CharField(source="assigned_to.get_full_name", read_only=True, default=None)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    effective_status = serializers.CharField(read_only=True)
    vendor_names = serializers.SerializerMethodField()

    class Meta:
        model = MaintenanceSchedule
        fields = [
            "id", "title", "maintenance_type", "frequency", "priority",
            "device", "device_code", "device_name", "device_status", "site", "site_name",
            "assigned_to", "assigned_to_name", "vendors", "vendor_names",
            "next_due", "instructions", "required_components",
            "status", "status_display",
            "effective_status", "is_active",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_vendor_names(self, obj):
        return [v.name for v in obj.vendors.all()]

    def validate(self, attrs):
        # A finished one-off job stays finished: its completion record is the
        # history, and a new fault raises a new job. Without this, a stale copy
        # of the schedule on someone's screen, saved from the edit form,
        # silently reopened jobs that had already been closed out.
        instance = self.instance
        if (
            instance is not None
            and instance.status == MaintenanceSchedule.Status.COMPLETED
            and not instance.is_active
            and "status" in attrs
            and attrs["status"] != MaintenanceSchedule.Status.COMPLETED
        ):
            raise serializers.ValidationError({
                "status": "This job was completed and closed out — raise a new one for a new fault."
            })
        return attrs

    def validate_required_components(self, value):
        """What the visit takes along, picked from inventory where possible.

        Each row is a stock item, an opened unique product, or (for older
        schedules) a name typed by hand. The name is filled in from inventory,
        so the list reads the same however it was built.
        """
        from django.core.exceptions import ValidationError as DjangoValidationError

        from apps.inventory.models import InventoryItem, InventoryUnitType

        if not isinstance(value, list):
            raise serializers.ValidationError("Must be a list of components.")
        cleaned = []
        for row in value:
            if not isinstance(row, dict):
                raise serializers.ValidationError("Each component must be an object.")
            try:
                quantity = max(1, int(row.get("quantity", 1)))
            except (TypeError, ValueError):
                quantity = 1
            name = str(row.get("name", "")).strip()
            entry = {"quantity": quantity}
            try:
                if row.get("inventory_item"):
                    item = (
                        InventoryItem.objects.select_related("material_type")
                        .filter(pk=row["inventory_item"]).first()
                    )
                    if item is None:
                        raise serializers.ValidationError("That stock item no longer exists.")
                    entry["inventory_item"] = str(item.pk)
                    entry["name"] = name or (item.material_type.name if item.material_type_id else item.sku)
                elif row.get("inventory_unit_type"):
                    product = InventoryUnitType.objects.filter(pk=row["inventory_unit_type"]).first()
                    if product is None:
                        raise serializers.ValidationError("That product no longer exists.")
                    entry["inventory_unit_type"] = str(product.pk)
                    entry["name"] = name or str(product)
                elif name:
                    entry["name"] = name
                else:
                    raise serializers.ValidationError(
                        "Each component needs an inventory item — or at least a name."
                    )
            except (DjangoValidationError, ValueError):
                raise serializers.ValidationError("That inventory reference is not valid.")
            entry["name"] = entry["name"][:200]
            cleaned.append(entry)
        return cleaned


class MaintenanceRecordPhotoSerializer(serializers.ModelSerializer):
    class Meta:
        model = MaintenanceRecordPhoto
        fields = ["id", "record", "image", "caption", "taken_by", "created_at"]
        read_only_fields = ["id", "taken_by", "created_at"]


class MaintenanceRecordSerializer(serializers.ModelSerializer):
    schedule_title = serializers.CharField(source="schedule.title", read_only=True, default=None)
    performed_by_name = serializers.CharField(source="performed_by.get_full_name", read_only=True, default=None)
    component_names = serializers.SerializerMethodField()
    photos = MaintenanceRecordPhotoSerializer(many=True, read_only=True)

    class Meta:
        model = MaintenanceRecord
        fields = [
            "id", "schedule", "schedule_title",
            "performed_by", "performed_by_name",
            "performed_at", "status", "notes", "cost",
            "is_billable", "charge_to",
            "components_used", "component_names", "photos",
            "created_at",
        ]
        read_only_fields = ["id", "performed_by", "created_at"]

    def get_component_names(self, obj):
        return [c.name for c in obj.components_used.all()]

    def create(self, validated_data):
        # Warranty-aware billability defaults (MW-01/02) — explicit payload
        # values win over the derived ones.
        from apps.warranties.services import derive_billability

        schedule = validated_data.get("schedule")
        device = schedule.device if schedule else None
        if device is not None:
            _, billable, charge = derive_billability(device)
            if "is_billable" not in validated_data:
                validated_data["is_billable"] = billable
            if not validated_data.get("charge_to"):
                validated_data["charge_to"] = charge
        return super().create(validated_data)

    def validate(self, attrs):
        schedule = attrs.get("schedule") or getattr(self.instance, "schedule", None)
        components = attrs.get("components_used") or []
        if schedule and schedule.device_id:
            for component in components:
                if component.device_id != schedule.device_id:
                    raise serializers.ValidationError(
                        {"components_used": "All components must belong to the schedule's asset."}
                    )
        elif components:
            raise serializers.ValidationError(
                {"components_used": "This schedule has no asset — components cannot be attached."}
            )
        return attrs
