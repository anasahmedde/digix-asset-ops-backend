from rest_framework import serializers

from .models import Warranty, WarrantyClaim
from common.dates import DateOrder


class WarrantySerializer(DateOrder, serializers.ModelSerializer):
    date_order = (("end_date", "start_date", "the start date"),)
    device_code = serializers.CharField(source="device.asset_code", read_only=True, default=None)
    device_name = serializers.CharField(source="device.display_name", read_only=True, default=None)
    component_name = serializers.CharField(source="component.name", read_only=True, default=None)
    supplier_name = serializers.CharField(source="supplier.name", read_only=True, default=None)
    # Who the asset belongs to and where it stands, so every warranty list
    # can say who is covered and where.
    client_name = serializers.SerializerMethodField()
    site_name = serializers.CharField(source="device.current_site.name", read_only=True, default=None)
    is_expired = serializers.BooleanField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    # Optional on create — validate() anchors them (procurement date for
    # supplier-side, handover/today for client) and derives end from months.
    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False)
    warranty_type_display = serializers.CharField(source="get_warranty_type_display", read_only=True)

    class Meta:
        model = Warranty
        fields = [
            "id", "device", "device_code", "device_name", "component", "component_name",
            "supplier", "supplier_name", "client_name", "site_name",
            "warranty_type", "warranty_type_display", "status", "status_display",
            "start_date", "end_date", "months", "reissued_from",
            "coverage_details", "reference_number", "vendor_reference", "notes",
            "is_expired", "created_at", "updated_at",
        ]
        # reissued_from is set exclusively by the /reissue/ action.
        # The reference is handed out by the system, never typed.
        read_only_fields = ["id", "reference_number", "reissued_from", "created_at", "updated_at"]

    def get_client_name(self, obj):
        client = obj.device.client_for
        return client.name if client is not None else None

    def validate(self, attrs):
        # The client's cover is given where the order is run, against the
        # project's own term, and starts the day the asset goes live.
        if self.instance is None and attrs.get("warranty_type") == Warranty.WarrantyType.CLIENT:
            raise serializers.ValidationError({"warranty_type": (
                "Client warranties are given from the project: Projects > Execution > Client Warranty."
            )})
        component = attrs.get("component")
        device = attrs.get("device") or getattr(self.instance, "device", None)
        if component and device and component.device_id != device.id:
            raise serializers.ValidationError({"component": "Component does not belong to this device."})

        # A part's cover is its supplier's: the one kind there is on a component.
        if component or (self.instance is not None and self.instance.component_id):
            attrs["warranty_type"] = Warranty.WarrantyType.SUPPLIER

        from django.utils import timezone as _tz

        end = attrs.get("end_date") or getattr(self.instance, "end_date", None)
        status = attrs.get("status") or getattr(self.instance, "status", Warranty.Status.ACTIVE)
        if end and end < _tz.localdate() and status == Warranty.Status.ACTIVE:
            attrs["status"] = Warranty.Status.EXPIRED

        if self.instance is None:
            # Cover from outside on the asset itself is the vendor's warranty,
            # whatever the paperwork calls it. Manufacturer and extended cover
            # belong to a component; only the client warranty is ours.
            if not component and attrs.get("warranty_type", Warranty.WarrantyType.MANUFACTURER) in (
                Warranty.WarrantyType.MANUFACTURER, Warranty.WarrantyType.EXTENDED,
            ):
                attrs["warranty_type"] = Warranty.WarrantyType.SUPPLIER

            # Anchoring defaults: supplier-side warranties start at the
            # procurement/delivery date, client warranties at handover (today
            # until the installation handover re-anchors them).
            from dateutil.relativedelta import relativedelta
            from django.utils import timezone

            warranty_type = attrs.get("warranty_type") or Warranty.WarrantyType.MANUFACTURER
            if not attrs.get("start_date"):
                if warranty_type == Warranty.WarrantyType.CLIENT:
                    attrs["start_date"] = timezone.now().date()
                else:
                    attrs["start_date"] = (device.purchase_date if device else None) or timezone.now().date()
            if not attrs.get("end_date"):
                months = attrs.get("months")
                if not months:
                    raise serializers.ValidationError({"end_date": "Provide an end date or a months term."})
                attrs["end_date"] = attrs["start_date"] + relativedelta(months=months)

        start = attrs.get("start_date") or getattr(self.instance, "start_date", None)
        end = attrs.get("end_date") or getattr(self.instance, "end_date", None)
        if start and end and end <= start:
            raise serializers.ValidationError({"end_date": "The end date has to come after the start date."})
        return attrs

    def update(self, instance, validated_data):
        # A warranty is bound to its device/component for life; ignore reassignment.
        validated_data.pop("device", None)
        validated_data.pop("component", None)
        # The vendor's cover on a finished asset is the vendor's, full stop:
        # its type does not change. Longer cover is recorded as an extension.
        new_type = validated_data.get("warranty_type")
        if (
            instance.warranty_type == Warranty.WarrantyType.SUPPLIER
            and new_type
            and new_type != Warranty.WarrantyType.SUPPLIER
        ):
            raise serializers.ValidationError({
                "warranty_type": "A vendor warranty stays a vendor warranty — extend it instead."
            })
        return super().update(instance, validated_data)


class WarrantyClaimSerializer(serializers.ModelSerializer):
    """A claim on a vendor's cover. Raised here; moved along by /transition/."""

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    resolution_display = serializers.CharField(source="get_resolution_display", read_only=True)
    supplier_name = serializers.CharField(source="supplier.name", read_only=True, default=None)
    device_code = serializers.CharField(source="device.asset_code", read_only=True, default=None)
    raised_by_name = serializers.SerializerMethodField()
    # What is claimed on, in one line, and the cover it is claimed under.
    kind = serializers.SerializerMethodField()
    covered = serializers.SerializerMethodField()
    covered_name = serializers.SerializerMethodField()
    cover_reference = serializers.SerializerMethodField()
    cover_end = serializers.SerializerMethodField()
    next_steps = serializers.SerializerMethodField()

    class Meta:
        model = WarrantyClaim
        fields = [
            "id", "claim_number", "warranty", "inventory_unit", "device", "device_code",
            "supplier", "supplier_name", "kind", "covered", "covered_name", "cover_reference", "cover_end",
            "fault", "description", "failure_date", "expected_cost", "evidence",
            "status", "status_display", "vendor_reference", "resolution", "resolution_display",
            "recovered_amount", "history", "raised_by", "raised_by_name",
            "submitted_at", "decided_at", "closed_at", "next_steps", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "claim_number", "device", "status", "vendor_reference", "resolution", "recovered_amount",
            "history", "raised_by", "submitted_at", "decided_at", "closed_at", "created_at", "updated_at",
        ]

    def get_raised_by_name(self, obj):
        u = obj.raised_by
        return (u.get_full_name() or u.username) if u else None

    def get_kind(self, obj):
        return "Component" if obj.inventory_unit_id else "Vendor"

    def get_covered(self, obj):
        if obj.inventory_unit_id:
            return obj.inventory_unit.serial_number
        return obj.device.asset_code if obj.device_id else None

    def get_covered_name(self, obj):
        if obj.inventory_unit_id:
            u = obj.inventory_unit
            return (u.unit_type.name if u.unit_type_id else None) or u.model_name or None
        return obj.device.display_name if obj.device_id else None

    def get_cover_reference(self, obj):
        if obj.inventory_unit_id:
            return obj.inventory_unit.warranty_reference or None
        return obj.warranty.reference_number if obj.warranty_id else None

    def get_cover_end(self, obj):
        if obj.inventory_unit_id:
            return obj.inventory_unit.warranty_end
        return obj.warranty.end_date if obj.warranty_id else None

    def get_next_steps(self, obj):
        return list(WarrantyClaim.NEXT.get(obj.status, ()))

    def validate(self, attrs):
        from django.utils import timezone

        if self.instance is not None:
            return attrs
        warranty, unit = attrs.get("warranty"), attrs.get("inventory_unit")
        if bool(warranty) == bool(unit):
            raise serializers.ValidationError(
                {"warranty": "Claim on one thing: an asset's vendor warranty or a part's component warranty."}
            )
        failed = attrs["failure_date"]
        if failed > timezone.localdate():
            raise serializers.ValidationError({"failure_date": "The failure cannot be in the future."})
        if warranty:
            if warranty.warranty_type == Warranty.WarrantyType.CLIENT:
                raise serializers.ValidationError(
                    {"warranty": "A client warranty is ours to honour, not one to claim on."}
                )
            if warranty.status in (Warranty.Status.VOID, Warranty.Status.REISSUED):
                raise serializers.ValidationError({"warranty": "That warranty is no longer in force."})
            start, end = warranty.start_date, warranty.end_date
            attrs["device"] = warranty.device
            attrs.setdefault("supplier", warranty.supplier)
        else:
            if not (unit.has_warranty and unit.warranty_end):
                raise serializers.ValidationError({"inventory_unit": "That part has no warranty to claim on."})
            start, end = unit.warranty_start, unit.warranty_end
            fitted = getattr(unit, "asset_component", None)
            attrs["device"] = fitted.device if fitted is not None else None
            attrs.setdefault("supplier", unit.supplier)
        # The fault has to have happened while the cover ran.
        if (start and failed < start) or failed > end:
            raise serializers.ValidationError({"failure_date": (
                f"It failed outside the cover ({start:%d %b %Y} to {end:%d %b %Y}); "
                "the vendor will not accept the claim."
            )})
        if not attrs.get("supplier"):
            raise serializers.ValidationError({"supplier": "Say which vendor the claim goes to."})
        return attrs
