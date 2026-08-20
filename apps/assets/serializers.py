from django.utils import timezone
from rest_framework import serializers

from .models import (
    AssetCode,
    AssetComponent,
    AssetType,
    Brand,
    Device,
    DeviceImage,
    DeviceLifecycleEvent,
    DeviceModel,
    MaterialType,
)


def _warranty_status(device) -> str:
    """none / active / expired for the device's warranties (prefetch-friendly)."""
    warranties = list(device.warranties.all())
    if not warranties:
        return "none"
    today = timezone.now().date()
    if any(w.status == "active" and w.end_date >= today for w in warranties):
        return "active"
    return "expired"


class AssetTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = AssetType
        fields = [
            "id", "name", "code", "description", "has_dimensions",
            "has_diagonal", "icon", "is_active", "created_at",
        ]
        read_only_fields = ["id", "code", "created_at"]


class BrandSerializer(serializers.ModelSerializer):
    class Meta:
        model = Brand
        fields = ["id", "name", "website", "logo", "is_active", "created_at"]
        read_only_fields = ["id", "created_at"]


class DeviceModelSerializer(serializers.ModelSerializer):
    brand_name = serializers.CharField(source="brand.name", read_only=True)

    class Meta:
        model = DeviceModel
        fields = [
            "id", "brand", "brand_name", "name", "model_number",
            "screen_type", "screen_size", "specifications", "is_active", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class MaterialTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = MaterialType
        fields = ["id", "name", "category", "unit", "description", "created_at"]
        read_only_fields = ["id", "created_at"]


class DeviceImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeviceImage
        fields = ["id", "device", "image", "caption", "is_primary", "sort_order", "created_at"]
        read_only_fields = ["id", "created_at"]


class AssetComponentSerializer(serializers.ModelSerializer):
    class Meta:
        model = AssetComponent
        fields = [
            "id", "device", "name", "component_type", "serial_number",
            "quantity", "notes", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


def _client_names(device):
    """Primary client first, then any additional shared clients."""
    names = []
    if device.assigned_client:
        names.append(device.assigned_client.name)
    for client in device.clients.all():
        if client.name not in names:
            names.append(client.name)
    return names


class DeviceListSerializer(serializers.ModelSerializer):
    device_model_name = serializers.CharField(source="device_model.__str__", read_only=True)
    asset_type_name = serializers.CharField(source="asset_type.name", read_only=True, default=None)
    site_name = serializers.CharField(source="current_site.name", read_only=True, default=None)
    client_name = serializers.CharField(source="assigned_client.name", read_only=True, default=None)
    client_names = serializers.SerializerMethodField()
    project_name = serializers.CharField(source="project.name", read_only=True, default=None)
    warranty_status = serializers.SerializerMethodField()

    class Meta:
        model = Device
        fields = [
            "id", "asset_code", "serial_number", "display_name", "project", "project_name",
            "asset_type", "asset_type_name",
            "device_model", "device_model_name",
            "status", "image", "current_site", "site_name",
            "assigned_client", "client_name", "client_names",
            "installation_date", "warranty_status", "created_at",
        ]

    def get_client_names(self, obj):
        return _client_names(obj)

    def get_warranty_status(self, obj):
        return _warranty_status(obj)


class DeviceDetailSerializer(serializers.ModelSerializer):
    device_model_name = serializers.CharField(source="device_model.__str__", read_only=True)
    asset_type_name = serializers.CharField(source="asset_type.name", read_only=True, default=None)
    brand_name = serializers.CharField(source="device_model.brand.name", read_only=True, default=None)
    screen_type = serializers.CharField(source="device_model.screen_type", read_only=True, default=None)
    screen_size = serializers.CharField(source="device_model.screen_size", read_only=True, default=None)
    specifications = serializers.JSONField(source="device_model.specifications", read_only=True, default=dict)
    site_name = serializers.CharField(source="current_site.name", read_only=True, default=None)
    client_name = serializers.CharField(source="assigned_client.name", read_only=True, default=None)
    client_names = serializers.SerializerMethodField()
    project_name = serializers.CharField(source="project.name", read_only=True, default=None)
    components = AssetComponentSerializer(many=True, read_only=True)
    supplier_name = serializers.CharField(source="supplier.name", read_only=True, default=None)
    technician_name = serializers.CharField(source="assigned_technician.get_full_name", read_only=True, default=None)
    installed_by_name = serializers.CharField(source="installed_by.get_full_name", read_only=True, default=None)
    images = DeviceImageSerializer(many=True, read_only=True)
    lifecycle_events = serializers.SerializerMethodField()
    warranty_status = serializers.SerializerMethodField()
    active_warranty = serializers.SerializerMethodField()
    tickets_total = serializers.SerializerMethodField()
    tickets_open = serializers.SerializerMethodField()
    # Client warranty term chosen at registration; creates a Warranty row
    # (type=client) that the beat task auto-completes after the term lapses.
    client_warranty_months = serializers.ChoiceField(
        choices=[3, 6, 12], write_only=True, required=False, allow_null=True
    )

    class Meta:
        model = Device
        fields = [
            "id", "asset_code", "serial_number", "mobile_id", "mac_address", "imei",
            "display_name", "asset_type", "asset_type_name",
            "device_model", "device_model_name", "brand_name", "screen_type", "screen_size",
            "length_cm", "width_cm", "diagonal_inches",
            "specifications", "firmware_version", "hardware_revision",
            "status", "image", "images",
            "purchase_date", "purchase_price", "supplier", "supplier_name",
            "invoice_reference", "batch_number",
            "current_site", "site_name", "assigned_client", "client_name",
            "clients", "client_names",
            "project", "project_name", "components",
            "assigned_technician", "technician_name",
            "installation_date", "installed_by", "installed_by_name",
            "warranty_status", "active_warranty",
            "tickets_total", "tickets_open", "client_warranty_months",
            "notes", "lifecycle_events", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "asset_code", "created_at", "updated_at"]

    def create(self, validated_data):
        months = validated_data.pop("client_warranty_months", None)
        device = super().create(validated_data)
        if months:
            from dateutil.relativedelta import relativedelta

            from apps.warranties.models import Warranty

            start = device.installation_date or device.purchase_date or timezone.now().date()
            Warranty.objects.create(
                device=device,
                supplier=device.supplier,
                warranty_type=Warranty.WarrantyType.CLIENT,
                start_date=start,
                end_date=start + relativedelta(months=months),
                months=months,
            )
        return device

    def update(self, instance, validated_data):
        validated_data.pop("client_warranty_months", None)
        return super().update(instance, validated_data)

    def get_client_names(self, obj):
        return _client_names(obj)

    def get_lifecycle_events(self, obj):
        events = obj.lifecycle_events.order_by("-created_at")[:10]
        return DeviceLifecycleEventSerializer(events, many=True).data

    def get_tickets_total(self, obj):
        return obj.tickets.count()

    def get_tickets_open(self, obj):
        return obj.tickets.exclude(status__in=["closed", "approved", "rejected"]).count()

    def get_warranty_status(self, obj):
        return _warranty_status(obj)

    def get_active_warranty(self, obj):
        today = timezone.now().date()
        warranties = list(obj.warranties.all())
        current = next(
            (w for w in warranties if w.status == "active" and w.end_date >= today), None
        )
        w = current or (sorted(warranties, key=lambda x: x.end_date, reverse=True)[0] if warranties else None)
        if w is None:
            return None
        return {
            "id": str(w.id),
            "warranty_type": w.warranty_type,
            "status": w.status,
            "start_date": w.start_date,
            "end_date": w.end_date,
            "supplier": str(w.supplier_id) if w.supplier_id else None,
            "supplier_name": w.supplier.name if w.supplier_id else None,
            "is_expired": w.is_expired,
        }


class DeviceLifecycleEventSerializer(serializers.ModelSerializer):
    performed_by_name = serializers.CharField(source="performed_by.get_full_name", read_only=True, default=None)

    class Meta:
        model = DeviceLifecycleEvent
        fields = [
            "id", "device", "event_type", "from_value", "to_value",
            "description", "performed_by", "performed_by_name", "metadata", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class AssetCodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = AssetCode
        fields = [
            "id", "device", "format", "label_size", "generated_file",
            "is_current", "printed_at", "created_at",
        ]
        read_only_fields = ["id", "generated_file", "created_at"]
