from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import serializers

from apps.sites.models import Site

from .models import (
    AssetCode,
    AssetComponent,
    AssetType,
    ComponentTemplate,
    Brand,
    Device,
    DeviceImage,
    DeviceLifecycleEvent,
    DeviceModel,
    MaterialType,
    ProductionRouteTemplate,
    ProductionStep,
)


def validate_assignee(attrs, *, allow_both=False):
    """Who installs the asset: a technician of ours or an external vendor.

    One or the other, except on a turnkey job where the vendor does the work
    and our technician oversees it, so the pair is deliberate. The vendor who
    *supplied* the asset is a separate thing and lives in its own fields.

    Shared by the status transition (when moving to `assigned`), the reassign
    action and the edit form, so all three enforce the same rule and wording.
    """
    technician = attrs.get("assigned_technician")
    vendor = (attrs.get("assigned_vendor_name") or "").strip()
    if not technician and not vendor:
        raise serializers.ValidationError({
            "assigned_technician": "Say who this asset is assigned to — a technician or a vendor."
        })
    if technician and vendor and not allow_both:
        raise serializers.ValidationError({
            "assigned_vendor_name": (
                "Installation goes to a technician or a vendor, not both — unless the asset "
                "is vendor supplied and installed, where the technician oversees the work."
            )
        })
    return attrs


def assignee_label(technician, vendor_name="", vendor_contact="") -> str:
    """Human-readable assignee for journal entries."""
    if technician:
        label = technician.get_full_name() or technician.username
        if technician.employee_id:
            label = f"{label} ({technician.employee_id})"
        return f"technician {label}"
    vendor = (vendor_name or "").strip()
    contact = (vendor_contact or "").strip()
    return f"vendor {vendor}" + (f" — {contact}" if contact else "")


# An asset carries two warranties of its own, on top of whatever cover the
# individual components brought with them from inventory. Keyed by who gives
# it, because "manufacturer/supplier/client" alone does not say that.
ASSET_WARRANTY_TYPES = {
    # We warrant the installed asset to the client who received it.
    "client": "client",
    # The vendor warrants the finished asset to us — only exists when we did
    # not build it ourselves.
    "vendor": "supplier",
}


def _asset_warranty(device, kind):
    """The asset-level warranty of one kind, if there is one.

    Component-scoped rows are excluded deliberately: a screen's own 12-month
    manufacturer cover is not the warranty we gave the client.
    """
    from apps.warranties.models import Warranty

    return next(
        (
            w for w in device.warranties.all()
            if w.component_id is None
            and w.warranty_type == ASSET_WARRANTY_TYPES[kind]
            and w.status in (Warranty.Status.ACTIVE, Warranty.Status.REISSUED, Warranty.Status.EXPIRED)
        ),
        None,
    )


def _asset_warranty_summary(device, kind):
    warranty = _asset_warranty(device, kind)
    if warranty is None:
        return None
    return {
        "id": str(warranty.id),
        "start_date": warranty.start_date,
        "end_date": warranty.end_date,
        "months": warranty.months,
        "status": warranty.status,
    }


def copy_build_definition(source, target):
    """Give `target` the same parts list and production route as `source`.

    Only the definition is copied — what the asset is built from and how —
    never what has happened to the source (issued stock, PO links, progress).
    """
    if target.source != Device.Source.INHOUSE:
        return
    for component in source.components.all():
        AssetComponent.objects.create(
            device=target,
            name=component.name,
            component_type=component.component_type,
            quantity=component.quantity,
            supplier=component.supplier,
            inventory_item=component.inventory_item,
            inventory_unit_type=component.inventory_unit_type,
            unit=component.unit,
            planned_unit_price=component.planned_unit_price,
        )
    for step in source.production_steps.all().order_by("step_number"):
        ProductionStep.objects.create(
            device=target,
            step_number=step.step_number,
            name=step.name,
            # The operations carry over; where each happens does not. That is
            # the project's decision for this asset, made in Execution.
            expected_days=step.expected_days,
            planned_cost=step.planned_cost,
        )


def _upsert_asset_warranty(device, kind, *, end=None, months=None):
    """Record (or move) one of the asset's own warranties.

    Expiry is what the paperwork states, so the term is derived from it and
    rounded to whole months the way a warranty period is quoted. Passing
    nothing leaves any existing cover alone — editing an asset should not
    silently drop its warranty.
    """
    if not (end or months):
        return None

    from dateutil.relativedelta import relativedelta

    from apps.warranties.models import Warranty

    if kind == "vendor" and device.source == Device.Source.INHOUSE:
        return None

    start = device.installation_date or device.purchase_date or timezone.now().date()
    if end:
        delta = relativedelta(end, start)
        months = max(1, delta.years * 12 + delta.months + (1 if delta.days else 0))
    else:
        end = start + relativedelta(months=months)

    existing = _asset_warranty(device, kind)
    if existing is not None:
        existing.end_date = end
        existing.months = months
        existing.save(update_fields=["end_date", "months", "updated_at"])
        return existing

    warranty = Warranty.objects.create(
        device=device,
        supplier=device.supplier,
        warranty_type=ASSET_WARRANTY_TYPES[kind],
        start_date=start,
        end_date=end,
        months=months,
    )
    # The cached relation would otherwise hide what we just created.
    device.refresh_from_db()
    return warranty


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
    """A component definition — name, category and unit of measure."""

    category_name = serializers.CharField(source="category.name", read_only=True, default=None)

    class Meta:
        model = MaterialType
        fields = ["id", "name", "category", "category_name", "unit", "description", "created_at"]
        read_only_fields = ["id", "created_at"]


class DeviceImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeviceImage
        fields = ["id", "device", "image", "caption", "is_primary", "sort_order", "created_at"]
        read_only_fields = ["id", "created_at"]


class AssetComponentSerializer(serializers.ModelSerializer):
    supplier_name = serializers.CharField(source="supplier.name", read_only=True, default=None)
    # Read-only here: cover is recorded in the Warranties section, and for a
    # part it is created automatically when the part is received.
    active_warranty = serializers.SerializerMethodField()
    # Where the parts came from. Components are drawn from the warehouse:
    # either generic stock (inventory_item + quantity) or one serialized unit.
    inventory_item_name = serializers.CharField(
        source="inventory_item.material_type.name", read_only=True, default=None
    )
    inventory_item_sku = serializers.CharField(source="inventory_item.sku", read_only=True, default=None)
    inventory_unit_code = serializers.CharField(source="inventory_unit.unit_code", read_only=True, default=None)
    # StringRelatedField, not source="…__str__": with no related row DRF walks
    # to a bound method-wrapper on None and renders it verbatim.
    inventory_unit_type_name = serializers.StringRelatedField(
        source="inventory_unit_type", read_only=True
    )
    # What the warehouse holds right now — shown for information only; it never
    # limits what you can require.
    available_quantity = serializers.SerializerMethodField()
    stock_requested_quantity = serializers.IntegerField(read_only=True)
    procure_quantity = serializers.IntegerField(read_only=True)
    undecided_quantity = serializers.IntegerField(read_only=True)
    outstanding_quantity = serializers.IntegerField(read_only=True)
    po_number = serializers.CharField(
        source="purchase_order_item.purchase_order.po_number", read_only=True, default=None
    )
    source_label = serializers.SerializerMethodField()
    increase_requested_by_name = serializers.CharField(
        source="increase_requested_by.get_full_name", read_only=True, default=None
    )

    class Meta:
        model = AssetComponent
        fields = [
            "id", "device", "name", "component_type", "serial_number",
            "quantity", "supplier", "supplier_name",
            "inventory_item", "inventory_item_name", "inventory_item_sku",
            "inventory_unit_type", "inventory_unit_type_name", "available_quantity",
            "fulfilment", "issued_quantity", "outstanding_quantity", "stock_requested_quantity", "procure_quantity", "undecided_quantity",
            "purchase_order_item", "po_number", "planned_unit_price",
            "pending_increase", "increase_reason", "increase_notes",
            "increase_requested_by_name", "increase_requested_at",
            "inventory_unit", "inventory_unit_code", "source_label",
            "active_warranty", "unit",
            "notes", "created_at",
        ]
        read_only_fields = [
            "id", "created_at", "fulfilment", "issued_quantity",
            "purchase_order_item",
        ]
        extra_kwargs = {"name": {"required": False}}

    def get_available_quantity(self, obj):
        if obj.inventory_unit_type_id:
            return obj.inventory_unit_type.in_stock_count
        if obj.inventory_item_id:
            return obj.inventory_item.quantity
        return None

    def get_source_label(self, obj):
        if obj.inventory_unit_type_id:
            return f"Unique · {obj.inventory_unit_type}"
        if obj.inventory_unit_id:
            return f"Unique · {obj.inventory_unit.serial_number}"
        if obj.inventory_item_id:
            return f"Stock · {obj.inventory_item.sku}"
        return None

    def get_active_warranty(self, obj):
        w = next((w for w in obj.warranties.all() if w.status in ("active", "reissued")), None) or next(
            iter(obj.warranties.all()), None
        )
        if w is None:
            return None
        return {
            "id": str(w.id),
            "warranty_type": w.warranty_type,
            "status": w.status,
            "start_date": w.start_date,
            "end_date": w.end_date,
            "months": w.months,
        }

    def validate(self, attrs):
        # A component is what the asset NEEDS, not what has been taken from the
        # warehouse — so there is deliberately no availability check here. You
        # can specify a build long before the parts exist; the project decides
        # later whether to cover each line from stock or by procuring it.
        if self.instance is None:
            # Only an in-house build consumes our inventory. A vendor-supplied
            # asset arrives complete, so it has no bill of materials of ours.
            device = attrs.get("device")
            if device is not None and device.source != Device.Source.INHOUSE:
                raise serializers.ValidationError({
                    "device": (
                        f"{device.get_source_display()} assets arrive complete from the "
                        "vendor — components are only listed for in-house production."
                    )
                })
            item = attrs.get("inventory_item")
            product = attrs.get("inventory_unit_type")
            if not item and not product:
                raise serializers.ValidationError({
                    "inventory_item": (
                        "Pick the inventory item this asset needs — a generic stock "
                        "item or an opened unique product."
                    )
                })
            if item and product:
                raise serializers.ValidationError(
                    {"inventory_item": "Choose either a stock item or a unique product, not both."}
                )
            if (attrs.get("quantity") or 1) < 1:
                raise serializers.ValidationError({"quantity": "Quantity must be at least 1."})

            # The same component is one line with a quantity, not two lines.
            if device is not None:
                clash = device.components.filter(
                    inventory_item=item, inventory_unit_type=product
                ).first() if (item or product) else None
                if clash is not None:
                    raise serializers.ValidationError({
                        "inventory_item": (
                            f"'{clash.name}' is already on this asset (×{clash.quantity}) — "
                            "increase its quantity instead of adding it again."
                        )
                    })

            # Describe the requirement from whichever record it points at.
            source = product or item
            if not attrs.get("unit"):
                attrs["unit"] = (
                    product.unit if product
                    else (source.material_type.unit if source.material_type_id else "")
                ) or "piece"
            if not attrs.get("name"):
                if product:
                    attrs["name"] = str(product)
                else:
                    attrs["name"] = (
                        source.material_type.name if source.material_type_id else source.sku
                    )
            if not attrs.get("component_type"):
                category = getattr(source, "category", None)
                attrs["component_type"] = category.name if category else ""
            if not attrs.get("supplier"):
                attrs["supplier"] = getattr(source, "supplier", None)
        return attrs

    # Requirements do not move stock, so nothing is consumed on create; and
    # they carry no warranty of their own — the part's cover is registered
    # when the part is received.


def _client_names(device):
    """Primary client first, then any additional shared clients."""
    names = []
    if device.assigned_client:
        names.append(device.assigned_client.name)
    for client in device.clients.all():
        if client.name not in names:
            names.append(client.name)
    return names


def _project_name(obj):
    """An asset belongs to a project either directly or through one of the
    project's scope lines, so both have to be looked at."""
    if obj.project_id:
        return obj.project.name
    for item in obj.project_scope_items.all():
        if item.project_id:
            return item.project.name
    return None


class DeviceListSerializer(serializers.ModelSerializer):
    device_model_name = serializers.StringRelatedField(source="device_model", read_only=True)
    asset_type_name = serializers.CharField(source="asset_type.name", read_only=True, default=None)
    site_name = serializers.CharField(source="current_site.name", read_only=True, default=None)
    client_name = serializers.CharField(source="assigned_client.name", read_only=True, default=None)
    client_names = serializers.SerializerMethodField()
    project_name = serializers.SerializerMethodField()
    warranty_status = serializers.SerializerMethodField()

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    class Meta:
        model = Device
        fields = [
            "id", "asset_code", "serial_number", "display_name", "project", "project_name",
            "asset_type", "asset_type_name",
            "device_model", "device_model_name",
            "status", "status_display", "source", "image", "current_site", "site_name",
            "assigned_client", "client_name", "client_names",
            "installation_date", "warranty_status", "created_at",
        ]

    def get_client_names(self, obj):
        return _client_names(obj)

    def get_project_name(self, obj):
        return _project_name(obj)

    def get_warranty_status(self, obj):
        return _warranty_status(obj)


class DeviceDetailSerializer(serializers.ModelSerializer):
    device_model_name = serializers.StringRelatedField(source="device_model", read_only=True)
    asset_type_name = serializers.CharField(source="asset_type.name", read_only=True, default=None)
    brand_name = serializers.CharField(source="device_model.brand.name", read_only=True, default=None)
    site_name = serializers.CharField(source="current_site.name", read_only=True, default=None)
    client_name = serializers.CharField(source="assigned_client.name", read_only=True, default=None)
    client_names = serializers.SerializerMethodField()
    project_name = serializers.SerializerMethodField()
    # Contract chip (PR-01): rental/sold context from the parent project.
    project_contract_type = serializers.CharField(
        source="project.contract_type", read_only=True, default=None
    )
    project_rental_end_date = serializers.DateField(
        source="project.rental_end_date", read_only=True, default=None
    )
    components = AssetComponentSerializer(many=True, read_only=True)
    supplier_name = serializers.CharField(source="supplier.name", read_only=True, default=None)
    technician_name = serializers.CharField(source="assigned_technician.get_full_name", read_only=True, default=None)
    # Assignment detail pulled from the manpower record, plus the vendor
    # alternative for assets handed to an external crew.
    technician_employee_id = serializers.CharField(
        source="assigned_technician.employee_id", read_only=True, default=None
    )
    technician_job_title = serializers.CharField(
        source="assigned_technician.job_title", read_only=True, default=None
    )
    technician_phone = serializers.CharField(
        source="assigned_technician.phone", read_only=True, default=None
    )
    assigned_to_display = serializers.SerializerMethodField()
    installed_by_name = serializers.CharField(source="installed_by.get_full_name", read_only=True, default=None)
    images = DeviceImageSerializer(many=True, read_only=True)
    lifecycle_events = serializers.SerializerMethodField()
    # When the asset last entered each status — printed under each node on
    # the lifecycle track, the way a shipment tracker dates its checkpoints.
    stage_dates = serializers.SerializerMethodField()
    warranty_status = serializers.SerializerMethodField()
    active_warranty = serializers.SerializerMethodField()
    tickets_total = serializers.SerializerMethodField()
    tickets_open = serializers.SerializerMethodField()
    # Client warranty term chosen at registration; creates a Warranty row
    # (type=client) that the beat task auto-completes after the term lapses.
    # Registration captures the expiry DATE; the term in months is derived from
    # it, which is how warranty paperwork actually reads. The old months-only
    # input is still accepted so existing integrations keep working.
    # Two warranties live on an asset and are easy to confuse, so they are
    # named for who gives them: the vendor warrants the asset to us (only on a
    # vendor route), and we warrant it to the client we installed it for.
    # Neither is a component warranty — those belong to the inventory line the
    # part came from and are read off the component.
    client_warranty_end = serializers.DateField(write_only=True, required=False, allow_null=True)
    vendor_warranty_end = serializers.DateField(write_only=True, required=False, allow_null=True)
    client_warranty = serializers.SerializerMethodField()
    vendor_warranty = serializers.SerializerMethodField()
    client_warranty_months = serializers.IntegerField(
        write_only=True, required=False, allow_null=True, min_value=1
    )
    # Legal next statuses from the machine — the UI renders these as guarded
    # transition buttons instead of a free select.
    source_display = serializers.CharField(source="get_source_display", read_only=True)
    # An in-house build has a production route; a vendor-built one does not.
    requires_production = serializers.SerializerMethodField()
    # On a turnkey job the vendor does the work and our technician oversees it,
    # so that asset legitimately carries both.
    requires_oversight = serializers.SerializerMethodField()
    production_steps = serializers.SerializerMethodField()
    # Has this asset type been produced before? If so its route can be reused.
    route_template_available = serializers.SerializerMethodField()
    component_template_available = serializers.SerializerMethodField()
    allowed_transitions = serializers.SerializerMethodField()
    # Frozen once the project is executing: components and route are read-only.
    is_locked = serializers.BooleanField(read_only=True)
    route_complete = serializers.BooleanField(read_only=True)
    # Register by copying an existing asset: its details are the form's
    # defaults, and its components and production route come across too.
    copy_from = serializers.PrimaryKeyRelatedField(
        queryset=Device.objects.all(), write_only=True, required=False, allow_null=True
    )
    procurement_po_number = serializers.CharField(
        source="procurement_item.purchase_order.po_number", read_only=True, default=None
    )

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    class Meta:
        model = Device
        fields = [
            "id", "asset_code", "serial_number",
            "display_name", "asset_type", "asset_type_name",
            # device_model stays readable for historical assets but is no longer
            # part of registration; identity comes from type + name + components.
            "device_model", "device_model_name", "brand_name",
            "length_in", "width_in", "depth_in", "diagonal_inches",
            "hardware_revision",
            "status", "status_display", "source", "source_display", "allowed_transitions", "image", "images",
            "purchase_date", "purchase_price", "supplier", "supplier_name",
            "invoice_reference", "batch_number",
            "current_site", "site_name", "assigned_client", "client_name",
            "clients", "client_names",
            "project", "project_name", "project_contract_type", "project_rental_end_date",
            "components",
            "assigned_technician", "technician_name", "technician_employee_id",
            "technician_job_title", "technician_phone",
            "assigned_vendor_name", "assigned_vendor_contact", "assigned_to_display",
            "supply_vendor_name", "supply_vendor_contact",
            "is_locked", "route_complete", "procurement_item", "procurement_po_number",
            "procurement_requested_at", "copy_from",
            "installation_date", "installed_by", "installed_by_name",
            "warranty_status", "active_warranty",
            "tickets_total", "tickets_open",
            "client_warranty_end", "client_warranty_months", "vendor_warranty_end",
            "client_warranty", "vendor_warranty",
            "production_steps", "requires_production", "requires_oversight",
            "route_template_available", "component_template_available",
            "notes", "lifecycle_events", "stage_dates", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "asset_code", "status", "created_at", "updated_at"]

    def create(self, validated_data):
        months = validated_data.pop("client_warranty_months", None)
        client_end = validated_data.pop("client_warranty_end", None)
        vendor_end = validated_data.pop("vendor_warranty_end", None)
        source = validated_data.pop("copy_from", None)
        device = super().create(validated_data)
        _upsert_asset_warranty(device, "client", end=client_end, months=months)
        _upsert_asset_warranty(device, "vendor", end=vendor_end)
        if source is not None:
            copy_build_definition(source, device)
        return device

    def get_client_warranty(self, obj):
        return _asset_warranty_summary(obj, "client")

    def get_vendor_warranty(self, obj):
        return _asset_warranty_summary(obj, "vendor")

    def validate(self, attrs):
        """Route rules, plus: an asset has one assignee — with one exception."""
        # A new asset cannot arrive with cover that has already lapsed; an
        # existing one can, because recording history is legitimate.
        if self.instance is None:
            for field in ("client_warranty_end", "vendor_warranty_end"):
                end = attrs.get(field)
                if end and end <= timezone.now().date():
                    raise serializers.ValidationError(
                        {field: "Warranty expiry must be in the future."}
                    )

        def current(name):
            return attrs[name] if name in attrs else getattr(self.instance, name, None)

        # An in-house build is ours end to end: nobody sold it to us and
        # nobody outside puts it up.
        route = current("source")
        if route == Device.Source.INHOUSE:
            if (current("supply_vendor_name") or "").strip():
                raise serializers.ValidationError({
                    "supply_vendor_name": "An in-house build is not bought from a vendor."
                })
            if (current("assigned_vendor_name") or "").strip():
                raise serializers.ValidationError({
                    "assigned_vendor_name": (
                        "An in-house build is installed by our own technician."
                    )
                })
        # Only a turnkey job has the vendor doing the installing as well.
        elif (
            route == Device.Source.VENDOR_SUPPLIED
            and (current("assigned_vendor_name") or "").strip()
        ):
            raise serializers.ValidationError({
                "assigned_vendor_name": (
                    "This route is installed by our own technician — switch to "
                    "'Vendor Supplied & Installed' if the vendor puts it up too."
                )
            })
        return attrs

    def update(self, instance, validated_data):
        months = validated_data.pop("client_warranty_months", None)
        client_end = validated_data.pop("client_warranty_end", None)
        vendor_end = validated_data.pop("vendor_warranty_end", None)
        # Status changes must go through the /transition/ action so the
        # machine is enforced and every flip is journalled with a reason.
        validated_data.pop("status", None)

        # Remember the assignee so a change made from the edit form is
        # journalled the same way the /reassign/ action would.
        before = assignee_label(
            instance.assigned_technician, instance.assigned_vendor_name, instance.assigned_vendor_contact
        ) if (instance.assigned_technician_id or instance.assigned_vendor_name) else ""

        device = super().update(instance, validated_data)

        after = assignee_label(
            device.assigned_technician, device.assigned_vendor_name, device.assigned_vendor_contact
        ) if (device.assigned_technician_id or device.assigned_vendor_name) else ""

        if before != after:
            request = self.context.get("request")
            DeviceLifecycleEvent.objects.create(
                device=device,
                event_type=DeviceLifecycleEvent.EventType.REASSIGNMENT,
                from_value=before,
                to_value=after,
                description=f"Assignment changed to {after}" if after else "Assignment cleared",
                performed_by=getattr(request, "user", None) if request else None,
            )

        _upsert_asset_warranty(device, "client", end=client_end, months=months)
        _upsert_asset_warranty(device, "vendor", end=vendor_end)
        return device

    def get_client_names(self, obj):
        return _client_names(obj)

    def get_project_name(self, obj):
        return _project_name(obj)

    def get_requires_production(self, obj):
        return obj.source == Device.Source.INHOUSE

    def get_requires_oversight(self, obj):
        return obj.source == Device.Source.VENDOR_TURNKEY

    def get_component_template_available(self, obj):
        if not obj.asset_type_id:
            return False
        return ComponentTemplate.objects.filter(asset_type_id=obj.asset_type_id).exists()

    def get_route_template_available(self, obj):
        if not obj.asset_type_id:
            return False
        return ProductionRouteTemplate.objects.filter(asset_type_id=obj.asset_type_id).exists()

    def get_production_steps(self, obj):
        return ProductionStepSerializer(obj.production_steps.all(), many=True).data

    def get_allowed_transitions(self, obj):
        # Going in and going live are recorded in the Installation Tracker, so
        # they are not offered as buttons here — the API refuses them either
        # way. Coming back into service after maintenance is registry work and
        # stays available. Kept in step with DeviceTransitionSerializer.
        return [
            status for status in Device.VALID_TRANSITIONS.get(obj.status, ())
            if (obj.status, status) not in DeviceTransitionSerializer.TRACKER_DRIVEN
            and obj.can_transition_to(status)
        ]

    def get_stage_dates(self, obj):
        from django.db.models import Max

        rows = (
            obj.lifecycle_events.filter(event_type=DeviceLifecycleEvent.EventType.STATUS_CHANGE)
            .exclude(to_value="")
            .values("to_value")
            .annotate(at=Max("created_at"))
        )
        return {row["to_value"]: row["at"] for row in rows}

    def get_lifecycle_events(self, obj):
        events = obj.lifecycle_events.order_by("-created_at")[:10]
        return DeviceLifecycleEventSerializer(events, many=True).data

    def get_tickets_total(self, obj):
        return obj.tickets.count()

    def get_tickets_open(self, obj):
        return obj.tickets.exclude(status__in=["closed", "approved", "rejected"]).count()

    def get_assigned_to_display(self, obj):
        """Who the asset is assigned to — technician or external vendor."""
        if obj.assigned_technician_id:
            tech = obj.assigned_technician
            label = tech.get_full_name() or tech.username
            detail = " · ".join(x for x in (tech.employee_id, tech.job_title) if x)
            return f"{label} · {detail}" if detail else label
        if obj.assigned_vendor_name:
            return (
                f"{obj.assigned_vendor_name} ({obj.assigned_vendor_contact})"
                if obj.assigned_vendor_contact else obj.assigned_vendor_name
            )
        return None

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


class DeviceTransitionSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=Device.Status.choices)
    reason = serializers.CharField()
    # Moving to `assigned` must say who it went to: an internal technician
    # (picked from the manpower records) or an external vendor typed by hand.
    assigned_technician = serializers.PrimaryKeyRelatedField(
        queryset=get_user_model().objects.all(), required=False, allow_null=True
    )
    assigned_vendor_name = serializers.CharField(required=False, allow_blank=True, max_length=200)
    assigned_vendor_contact = serializers.CharField(required=False, allow_blank=True, max_length=100)
    # Where the job happens. An installation record needs a site, and the
    # installation record is what puts the asset on the tracker, so the assign
    # step is where the site gets pinned down if it is not already.
    current_site = serializers.PrimaryKeyRelatedField(
        queryset=Site.objects.all(), required=False, allow_null=True
    )
    # Taking an asset out of service raises a corrective job, so the details
    # that job needs are asked for at the moment the asset goes down.
    maintenance_due = serializers.DateField(required=False, allow_null=True)
    maintenance_priority = serializers.ChoiceField(
        choices=["low", "medium", "high"], required=False, default="high"
    )
    maintenance_assigned_to = serializers.PrimaryKeyRelatedField(
        queryset=get_user_model().objects.all(), required=False, allow_null=True
    )
    maintenance_instructions = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_status(self, value):
        device = self.context["device"]
        if not device.can_transition_to(value):
            current = device.get_status_display()
            target = dict(Device.Status.choices).get(value, value)
            allowed = ", ".join(Device.VALID_TRANSITIONS.get(device.status, ())) or "none"
            raise serializers.ValidationError(
                f"Cannot transition from '{current}' to '{target}'. Allowed: {allowed}."
            )
        return value

    # Two moves are facts about the physical world, established by the
    # technician on site through the Installation Tracker: putting the asset in
    # (assigned -> installed) and switching it on (installed -> active). Typing
    # those into the registry would let the two records drift apart.
    #
    # Reaching the same statuses any other way is ordinary registry work — an
    # asset coming back into service after maintenance or repair was installed
    # long ago and has no site visit to record — so the rule is keyed on the
    # move, not on the destination.
    TRACKER_DRIVEN = {
        (Device.Status.ASSIGNED, Device.Status.INSTALLED),
        (Device.Status.INSTALLED, Device.Status.ACTIVE),
    }

    def validate(self, attrs):
        device = self.context["device"]
        target = attrs.get("status")

        if (device.status, target) in self.TRACKER_DRIVEN:
            raise serializers.ValidationError({
                "status": (
                    f"'{Device.Status(target).label}' is set from the Installation Tracker, not "
                    f"here — the technician on site records the work and the photo, and the "
                    f"registry follows."
                )
            })

        # An in-house asset is built out of inventory, so production cannot
        # start until the materials are actually in hand. A vendor-supplied one
        # arrives complete and has no parts list of ours to check.
        if target == Device.Status.IN_PRODUCTION and device.source == Device.Source.INHOUSE:
            components = list(device.components.all())
            if not components:
                raise serializers.ValidationError({
                    "status": (
                        "Add the components this asset is built from before moving it "
                        "into production — the Components section is empty."
                    )
                })
            # Every line has to be covered — from stock or by procurement —
            # before the floor can start. The project decides which; until it
            # has, and until the parts have actually been issued, the build
            # would stall halfway.
            short = [c for c in components if c.outstanding_quantity > 0]
            if short:
                names = ", ".join(
                    f"{c.name} (short {c.outstanding_quantity} of {c.quantity})" for c in short[:4]
                )
                if len(short) > 4:
                    names += f", and {len(short) - 4} more"
                raise serializers.ValidationError({
                    "status": (
                        "Production cannot start until every component is fulfilled from the "
                        f"Project section. Still outstanding: {names}."
                    )
                })

        if target == Device.Status.UNDER_MAINTENANCE:
            errors = {}
            due = attrs.get("maintenance_due")
            if due is None:
                errors["maintenance_due"] = "Give the date the repair has to be done by."
            elif due < timezone.localdate():
                errors["maintenance_due"] = "The due date cannot be in the past."
            if not attrs.get("maintenance_assigned_to"):
                errors["maintenance_assigned_to"] = "Say which technician is taking the job."
            if errors:
                raise serializers.ValidationError(errors)
            return attrs

        if target != Device.Status.ASSIGNED:
            return attrs

        # Who does the work is the first question; where it happens is the
        # second, and both have to be answered to open the job.
        attrs = validate_assignee(
            attrs, allow_both=device.source == Device.Source.VENDOR_TURNKEY
        )
        if not (attrs.get("current_site") or device.current_site_id):
            raise serializers.ValidationError({
                "current_site": (
                    "Say which site the asset is going to — assigning it opens its job "
                    "on the Installation Tracker, and a job happens somewhere."
                )
            })
        return attrs


class DeviceAssignmentSerializer(serializers.Serializer):
    """Change who an already-assigned asset is assigned to.

    Same technician-or-vendor rule as the transition, but without moving the
    status — used to hand an in-flight installation to someone else.
    """

    assigned_technician = serializers.PrimaryKeyRelatedField(
        queryset=get_user_model().objects.all(), required=False, allow_null=True
    )
    assigned_vendor_name = serializers.CharField(required=False, allow_blank=True, max_length=200)
    assigned_vendor_contact = serializers.CharField(required=False, allow_blank=True, max_length=100)
    reason = serializers.CharField()

    def validate(self, attrs):
        device = self.context.get("device")
        return validate_assignee(
            attrs,
            allow_both=device is not None and device.source == Device.Source.VENDOR_TURNKEY,
        )


class ProductionStepSerializer(serializers.ModelSerializer):
    """One operation in an in-house build route."""

    workshop_display = serializers.CharField(read_only=True)
    assigned_to_name = serializers.CharField(
        source="assigned_to.get_full_name", read_only=True, default=None
    )
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    location_display = serializers.CharField(source="get_location_display", read_only=True)
    allowed_transitions = serializers.SerializerMethodField()
    # The open work order for this operation, when it was given to a workshop.
    work_order = serializers.SerializerMethodField()

    def get_work_order(self, obj):
        o = obj.live_work_order
        if o is None:
            return None
        return {"id": str(o.pk), "wo_number": o.wo_number, "status": o.status,
                "status_display": o.get_status_display(), "amount": o.total_amount}

    # Execution asked for a work order; Work Orders › Requests has it.
    work_order_requested = serializers.BooleanField(read_only=True)

    class Meta:
        model = ProductionStep
        fields = [
            "id", "device", "step_number", "name",
            "location", "location_display", "workshop", "workshop_name", "workshop_display",
            "status", "status_display", "allowed_transitions", "hold_reason", "decision_pending", "work_order",
            "work_order_requested", "work_order_requested_at",
            "assigned_to", "assigned_to_name", "expected_days", "planned_cost", "actual_cost",
            "started_at", "sent_at", "returned_at", "completed_at",
            "notes", "created_at",
        ]
        read_only_fields = [
            "id", "status", "started_at", "sent_at", "returned_at", "completed_at", "created_at",
        ]
        # The model keeps the DB-level constraint; this hands the clash to
        # validate() so the error names the step_number field.
        validators = []

    def get_allowed_transitions(self, obj):
        return list(obj.manual_moves)

    hold_reason = serializers.CharField(read_only=True)
    # True while the project still has to say where this operation happens.
    decision_pending = serializers.SerializerMethodField()

    def get_decision_pending(self, obj):
        return obj.location == obj.Location.UNDECIDED and obj.on_project

    def validate(self, attrs):
        def current(name):
            return attrs[name] if name in attrs else getattr(self.instance, name, None)

        device = current("device")
        if device is not None and device.source != Device.Source.INHOUSE:
            raise serializers.ValidationError({
                "device": (
                    "Production steps only apply to in-house builds — this asset is "
                    f"'{device.get_source_display()}'."
                )
            })

        if current("location") == ProductionStep.Location.EXTERNAL:
            if not current("workshop") and not (current("workshop_name") or "").strip():
                raise serializers.ValidationError({
                    "workshop": "Say which workshop the work goes to, or name it by hand."
                })
        elif (current("workshop_name") or "").strip() or current("workshop"):
            raise serializers.ValidationError({
                "workshop": "An in-house step has no workshop — set the location to Outside Workshop."
            })

        # Keep the route sequential per asset.
        number = current("step_number")
        if device is not None and number is not None:
            clash = ProductionStep.objects.filter(device=device, step_number=number)
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError(
                    {"step_number": f"Step {number} already exists on this asset."}
                )
        return attrs


class ProductionStepTransitionSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=ProductionStep.Status.choices)
    notes = serializers.CharField(required=False, allow_blank=True)


class DeviceLifecycleEventSerializer(serializers.ModelSerializer):
    performed_by_name = serializers.SerializerMethodField()

    def get_performed_by_name(self, obj):
        """Who did it. Not everyone has a full name on file, and an unnamed
        entry in an activity feed is worse than the username."""
        user = obj.performed_by
        if user is None:
            return None
        return user.get_full_name() or user.username

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
