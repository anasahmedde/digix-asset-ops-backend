from rest_framework import serializers

from .models import (
    ReorderRequest,
    GoodsReceipt,
    GoodsReceiptLine,
    InventoryCategory,
    InventoryItem,
    InventoryUnit,
    InventoryUnitType,
    Issuance,
    IssuanceRequest,
    StockMovement,
)


class InventoryCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = InventoryCategory
        fields = ["id", "name", "description", "is_active", "created_at"]
        read_only_fields = ["id", "created_at"]


class InventoryItemSerializer(serializers.ModelSerializer):
    material_name = serializers.CharField(source="material_type.name", read_only=True, default=None)
    category_name = serializers.CharField(source="category.name", read_only=True, default=None)
    unit = serializers.CharField(source="material_type.unit", read_only=True, default=None)
    is_low_stock = serializers.BooleanField(read_only=True)
    total_value = serializers.SerializerMethodField()

    class Meta:
        model = InventoryItem
        fields = [
            "id", "material_type", "material_name", "category", "category_name",
            "sku", "quantity", "min_stock_level", "unit", "location", "storage_location",
            "unit_cost", "total_value", "watch_on_dashboard", "notes", "is_low_stock",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "sku", "created_at", "updated_at"]

    def get_total_value(self, obj):
        if obj.unit_cost is None:
            return None
        return obj.quantity * obj.unit_cost

    def validate_material_type(self, value):
        """One stock record per material.

        Opening a second row for a material that already has one splits its
        stock in two: goods received land on one row while requirements watch
        the other, and the quantity looks like it never moved.
        """
        clash = InventoryItem.objects.filter(material_type=value)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        existing = clash.first()
        if existing is not None:
            raise serializers.ValidationError(
                f"{value.name} is already in inventory as {existing.sku} "
                f"(quantity {existing.quantity}) — edit that item instead of opening a second one."
            )
        return value


class InventoryUnitTypeSerializer(serializers.ModelSerializer):
    """A unique product as opened in inventory — details now, serials later."""

    material_name = serializers.CharField(source="material_type.name", read_only=True, default=None)
    category_name = serializers.CharField(source="category.name", read_only=True, default=None)
    brand_name = serializers.CharField(source="brand.name", read_only=True, default=None)
    supplier_name = serializers.CharField(source="supplier.name", read_only=True, default=None)
    # Opened at zero and filled by goods receipt, so the count is derived.
    # Uses the list queryset's annotation when present, else the model property.
    in_stock_count = serializers.SerializerMethodField()
    # Stock already on the shelf when the product is first opened. Serialized
    # items need a serial each, typed here — one per unit, all different, none
    # already in inventory. Opening at zero needs none.
    opening_quantity = serializers.IntegerField(
        write_only=True, required=False, min_value=0, max_value=500, default=0
    )
    opening_serials = serializers.ListField(
        child=serializers.CharField(max_length=200, allow_blank=True),
        write_only=True, required=False, default=list,
    )

    class Meta:
        model = InventoryUnitType
        fields = [
            "id", "type_code", "name",
            "material_type", "material_name", "category", "category_name",
            "brand", "brand_name", "model_name", "unit", "specifications",
            "unit_cost", "min_stock_level", "is_high_value",
            "default_has_warranty", "default_warranty_type", "default_warranty_months",
            "supplier", "supplier_name",
            "in_stock_count", "opening_quantity", "opening_serials", "notes", "is_active",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "type_code", "created_at", "updated_at"]

    def get_in_stock_count(self, obj):
        return getattr(obj, "stock_count", None) or obj.in_stock_count

    def _validate_opening_stock(self, attrs):
        """Stock on the shelf needs a serial per unit; opened empty, none."""
        if self.instance is not None:
            return attrs
        opening = attrs.get("opening_quantity") or 0
        serials = [(sn or "").strip() for sn in attrs.get("opening_serials") or []]
        if opening == 0:
            attrs["opening_serials"] = []
            return attrs
        if len(serials) != opening or any(not sn for sn in serials):
            raise serializers.ValidationError({
                "opening_serials": [f"Type a serial number for each of the {opening} unit(s) on the shelf."],
            })
        seen, repeated = set(), []
        for sn in serials:
            key = sn.lower()
            if key in seen and sn not in repeated:
                repeated.append(sn)
            seen.add(key)
        if repeated:
            raise serializers.ValidationError({
                "opening_serials": [f"Each unit needs its own serial — repeated: {', '.join(repeated)}."],
            })
        from .models import InventoryUnit

        taken = list(InventoryUnit.objects.filter(serial_number__in=serials).values_list("serial_number", flat=True))
        if taken:
            raise serializers.ValidationError({
                "opening_serials": [f"Already in inventory: {', '.join(sorted(taken))}."],
            })
        attrs["opening_serials"] = serials
        return attrs

    def create(self, validated_data):
        opening = validated_data.pop("opening_quantity", 0) or 0
        serials = validated_data.pop("opening_serials", []) or []
        unit_type = super().create(validated_data)
        if opening:
            from .models import InventoryUnit

            # Saved one at a time, not bulk_create: each unit's unit_code is
            # generated in save(), and bulk_create would leave them all blank
            # against a unique column.
            for serial in serials:
                InventoryUnit.objects.create(
                    unit_type=unit_type,
                    serial_number=serial,
                    material_type=unit_type.material_type,
                    category=unit_type.category,
                    brand=unit_type.brand,
                    model_name=unit_type.model_name,
                    supplier=unit_type.supplier,
                    purchase_price=unit_type.unit_cost,
                    notes="Opening stock.",
                )
        return unit_type

    def update(self, instance, validated_data):
        # Opening stock is a fact about the moment the product was opened; it
        # is not something an edit can replay.
        validated_data.pop("opening_quantity", None)
        validated_data.pop("opening_serials", None)
        return super().update(instance, validated_data)

    def validate_name(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Give the product a name.")
        return value

    def validate(self, attrs):
        def current(name):
            return attrs[name] if name in attrs else getattr(self.instance, name, None)

        if current("default_has_warranty") and not current("default_warranty_months"):
            raise serializers.ValidationError({
                "default_warranty_months": "Set the warranty term in months, or clear the warranty default."
            })
        return self._validate_opening_stock(attrs)


class InventoryUnitSerializer(serializers.ModelSerializer):
    """Serialized ("unique") inventory items — one row per physical unit."""

    material_name = serializers.CharField(source="material_type.name", read_only=True, default=None)
    category_name = serializers.CharField(source="category.name", read_only=True, default=None)
    brand_name = serializers.CharField(source="brand.name", read_only=True, default=None)
    supplier_name = serializers.CharField(source="supplier.name", read_only=True, default=None)
    warranty_state = serializers.CharField(read_only=True)
    is_under_warranty = serializers.BooleanField(read_only=True)
    converted_device_code = serializers.CharField(
        source="converted_device.asset_code", read_only=True, default=None
    )
    # Traceability: batch number plus the delivery and PO it arrived on.
    # Not source="unit_type.__str__": with no product, DRF walks to a bound
    # method-wrapper on None and renders it verbatim instead of falling back.
    unit_type_name = serializers.SerializerMethodField()
    unit_type_code = serializers.CharField(source="unit_type.type_code", read_only=True, default=None)
    grn_number = serializers.CharField(
        source="goods_receipt_line.receipt.grn_number", read_only=True, default=None
    )
    po_number = serializers.CharField(
        source="goods_receipt_line.receipt.purchase_order.po_number", read_only=True, default=None
    )
    # Where the unit physically is: fitted into an asset, or still on the shelf.
    installed_in_code = serializers.SerializerMethodField()
    installed_in_name = serializers.SerializerMethodField()

    class Meta:
        model = InventoryUnit
        fields = [
            "id", "unit_code", "serial_number",
            "unit_type", "unit_type_name", "unit_type_code",
            "material_type", "material_name", "category", "category_name",
            "brand", "brand_name", "model_name",
            "status", "location", "installed_in_code", "installed_in_name",
            "supplier", "supplier_name", "purchase_date", "purchase_price", "batch_number",
            "goods_receipt_line", "grn_number", "po_number",
            "has_warranty", "warranty_type", "warranty_start", "warranty_months", "warranty_end",
            "warranty_state", "is_under_warranty",
            "converted_device", "converted_device_code",
            "notes", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "unit_code", "status", "converted_device", "goods_receipt_line",
            "created_at", "updated_at",
        ]

    def get_unit_type_name(self, obj):
        return str(obj.unit_type) if obj.unit_type_id else None

    def _installed_device(self, obj):
        """The asset this unit was fitted into, if it has been."""
        component = getattr(obj, "asset_component", None)
        return getattr(component, "device", None) if component else None

    def get_installed_in_code(self, obj):
        device = self._installed_device(obj)
        return device.asset_code if device else None

    def get_installed_in_name(self, obj):
        device = self._installed_device(obj)
        return (device.display_name or "") if device else None

    def validate_serial_number(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Serial number is required for a unique item.")
        return value

    def validate(self, attrs):
        # Reads fall back to the instance so PATCHes that touch only one
        # warranty field are still validated against the full picture.
        def current(name):
            if name in attrs:
                return attrs[name]
            return getattr(self.instance, name, None)

        # A unit is identified by a material type or by its make/model. Asked
        # when the unit is registered, not on every edit afterwards: opening
        # stock raises units from a product that may carry neither, and those
        # units still need their provisional serials corrected.
        identified = current("material_type") or (current("model_name") or "").strip()
        if self.instance is None and not identified:
            raise serializers.ValidationError(
                {"material_type": "Give the unit a material type, or a model name."}
            )

        if current("has_warranty"):
            # A part's cover comes from the supplier it was received from.
            attrs["warranty_type"] = "supplier"
            if not current("warranty_start"):
                raise serializers.ValidationError(
                    {"warranty_start": "A warranty needs a start date."}
                )
            if not current("warranty_end") and not current("warranty_months"):
                raise serializers.ValidationError(
                    {"warranty_end": "Provide an end date or a term in months."}
                )
            start, end = current("warranty_start"), current("warranty_end")
            if start and end and end < start:
                raise serializers.ValidationError(
                    {"warranty_end": "Warranty end date cannot be before the start date."}
                )
        return attrs


class InventoryUnitTransitionSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=InventoryUnit.Status.choices)
    notes = serializers.CharField(required=False, allow_blank=True)


class InventoryUnitBulkSerializer(InventoryUnitSerializer):
    """Register several identical unique items in one go.

    Each physical unit still becomes its own row with its own serial number —
    ``quantity`` is a data-entry convenience, not a field on the unit. Supply
    ``serial_numbers`` (one per unit), or a single ``serial_number`` which is
    suffixed ``-1 … -N``.
    """

    quantity = serializers.IntegerField(min_value=1, max_value=500, write_only=True)
    serial_numbers = serializers.ListField(
        child=serializers.CharField(max_length=200), write_only=True, required=False, allow_empty=True
    )

    class Meta(InventoryUnitSerializer.Meta):
        fields = InventoryUnitSerializer.Meta.fields + ["quantity", "serial_numbers"]
        extra_kwargs = {"serial_number": {"required": False}}

    def validate(self, attrs):
        quantity = attrs.get("quantity", 1)
        serials = [s.strip() for s in (attrs.get("serial_numbers") or []) if s.strip()]
        base = (attrs.get("serial_number") or "").strip()

        if serials:
            if len(serials) != quantity:
                raise serializers.ValidationError(
                    {"serial_numbers": f"Provide exactly {quantity} serial number(s); got {len(serials)}."}
                )
        elif base:
            serials = [base] if quantity == 1 else [f"{base}-{i}" for i in range(1, quantity + 1)]
        else:
            raise serializers.ValidationError(
                {"serial_number": "Provide a serial number, or one serial per unit in serial_numbers."}
            )

        if len(set(serials)) != len(serials):
            raise serializers.ValidationError({"serial_numbers": "Serial numbers must be unique."})

        clashes = list(
            InventoryUnit.objects.filter(serial_number__in=serials).values_list("serial_number", flat=True)[:5]
        )
        if clashes:
            raise serializers.ValidationError(
                {"serial_numbers": f"Already registered: {', '.join(clashes)}"}
            )

        attrs["serial_number"] = serials[0]
        # Run the parent's warranty checks against a single representative row.
        attrs = super().validate(attrs)
        attrs["_serials"] = serials
        return attrs

    def create(self, validated_data):
        serials = validated_data.pop("_serials")
        validated_data.pop("quantity", None)
        validated_data.pop("serial_numbers", None)
        validated_data.pop("serial_number", None)

        units = []
        for serial in serials:
            # Saved one at a time (not bulk_create) so unit_code generation and
            # the warranty-end derivation in save() run for every unit.
            units.append(InventoryUnit.objects.create(serial_number=serial, **validated_data))
        return units


class StockMovementSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.material_type.name", read_only=True, default=None)
    # Who moved it: their name, or their login when no name is on file.
    performed_by_name = serializers.SerializerMethodField()
    # Where this stock came from: the delivery, the order behind it, and who
    # supplied it. Without these a movement says a number changed but not why.
    grn_number = serializers.CharField(
        source="goods_receipt_line.receipt.grn_number", read_only=True, default=None
    )
    po_number = serializers.CharField(
        source="goods_receipt_line.receipt.purchase_order.po_number", read_only=True, default=None
    )
    supplier_name = serializers.CharField(
        source="goods_receipt_line.receipt.purchase_order.supplier.name", read_only=True, default=None
    )

    def get_performed_by_name(self, obj):
        user = obj.performed_by
        if user is None:
            return None
        return user.get_full_name() or user.username

    class Meta:
        model = StockMovement
        fields = [
            "id", "item", "item_name", "movement_type",
            "quantity", "reference", "notes", "batch_number",
            "goods_receipt_line", "grn_number", "po_number", "supplier_name",
            "performed_by", "performed_by_name", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class GoodsReceiptLineSerializer(serializers.ModelSerializer):
    po_item_description = serializers.CharField(source="po_item.description", read_only=True, default=None)
    inventory_item_name = serializers.CharField(
        source="inventory_item.material_type.name", read_only=True, default=None
    )
    # Traceability: which delivery and which purchase order this came from.
    grn_number = serializers.CharField(source="receipt.grn_number", read_only=True, default=None)
    po_number = serializers.CharField(
        source="receipt.purchase_order.po_number", read_only=True, default=None
    )
    supplier_name = serializers.CharField(
        source="receipt.purchase_order.supplier.name", read_only=True, default=None
    )
    material_type = serializers.PrimaryKeyRelatedField(
        source="po_item.material_type", read_only=True, default=None
    )
    material_name = serializers.CharField(
        source="po_item.material_type.name", read_only=True, default=None
    )
    device_model_name = serializers.CharField(
        source="po_item.device_model.name", read_only=True, default=None
    )
    inspected_by_name = serializers.CharField(
        source="inspected_by.get_full_name", read_only=True, default=None
    )
    stocked_unit_count = serializers.SerializerMethodField()
    # The unit the delivered quantity is counted in.
    unit = serializers.SerializerMethodField()
    # What the order line was bought for. A component is opened in inventory
    # before it is ever ordered, so by the time goods arrive the kind is known
    # and inspection only has to show it.
    kind = serializers.SerializerMethodField()
    known_component = serializers.SerializerMethodField()

    def get_kind(self, obj):
        po = obj.po_item
        if po is not None and po.inventory_unit_type_id:
            return "unique"
        if (po is not None and po.inventory_item_id) or obj.inventory_item_id:
            return "generic"
        if po is not None and po.procured_devices.exists():
            return "asset"
        return None

    def get_known_component(self, obj):
        po = obj.po_item
        if po is not None and po.inventory_unit_type_id:
            return po.inventory_unit_type.name
        item = (po.inventory_item if po is not None and po.inventory_item_id else None) or (
            obj.inventory_item if obj.inventory_item_id else None
        )
        if item is not None:
            name = item.material_type.name if item.material_type_id else item.sku
            return f"{name} · {item.sku}" if item.sku and name != item.sku else name
        return None

    def get_unit(self, obj):
        po = obj.po_item
        if po is not None and po.inventory_unit_type_id:
            return po.inventory_unit_type.unit or "piece"
        if obj.inventory_item_id and obj.inventory_item.material_type_id:
            return obj.inventory_item.material_type.unit or "piece"
        if po is not None and po.material_type_id:
            return po.material_type.unit or "piece"
        if po is not None and po.procured_devices.exists():
            return "asset"
        return "piece"

    class Meta:
        model = GoodsReceiptLine
        fields = [
            "id", "receipt", "grn_number", "po_number", "supplier_name",
            "po_item", "po_item_description", "material_type", "material_name", "device_model_name",
            "inventory_item", "inventory_item_name",
            "quantity", "unit", "batch_number", "serial_numbers",
            "inspection_status", "routed_to", "accepted_quantity", "rejected_quantity",
            "inspected_by", "inspected_by_name", "inspected_at", "inspection_notes",
            "stocked_unit_count", "kind", "known_component", "created_at",
        ]
        read_only_fields = fields

    def get_stocked_unit_count(self, obj):
        return obj.units.count()


class InventoryUnitIntakeSerializer(serializers.Serializer):
    """One unique unit as entered by the inspecting technician.

    When the product was already opened in inventory (``unit_type``), the
    serial number is the only thing that needs typing — make, model, technical
    details and warranty terms are inherited from that record.
    """

    serial_number = serializers.CharField(max_length=200)
    unit_type = serializers.UUIDField(required=False, allow_null=True)
    material_type = serializers.UUIDField(required=False, allow_null=True)
    category = serializers.UUIDField(required=False, allow_null=True)
    brand = serializers.UUIDField(required=False, allow_null=True)
    model_name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    supplier = serializers.UUIDField(required=False, allow_null=True)
    purchase_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )
    purchase_date = serializers.DateField(required=False, allow_null=True)
    has_warranty = serializers.BooleanField(required=False, default=False)
    warranty_type = serializers.ChoiceField(
        choices=InventoryUnit.WarrantyType.choices, required=False, allow_blank=True
    )
    warranty_start = serializers.DateField(required=False, allow_null=True)
    warranty_months = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    warranty_end = serializers.DateField(required=False, allow_null=True)


class GoodsReceiptLineInspectSerializer(serializers.Serializer):
    """A technician's inspection verdict on one received line."""

    route = serializers.ChoiceField(choices=GoodsReceiptLine.Route.choices, required=False)
    accepted_quantity = serializers.IntegerField(min_value=0)
    rejected_quantity = serializers.IntegerField(min_value=0, required=False, default=0)
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    # Generic route: optional overrides for the stock record being topped up.
    generic = serializers.DictField(required=False)
    # Unique route: one entry per accepted unit.
    units = InventoryUnitIntakeSerializer(many=True, required=False)

    def validate(self, attrs):
        line = self.context["line"]
        if not line.is_pending_inspection:
            raise serializers.ValidationError(
                {"detail": f"This line was already inspected ({line.get_inspection_status_display()})."}
            )

        accepted = attrs["accepted_quantity"]
        rejected = attrs.get("rejected_quantity", 0)
        if accepted + rejected != line.quantity:
            raise serializers.ValidationError({
                "accepted_quantity": (
                    f"Accepted plus rejected must equal the {line.quantity} received "
                    f"(got {accepted} + {rejected})."
                )
            })
        if accepted and not attrs.get("route"):
            raise serializers.ValidationError(
                {"route": "Choose where the accepted items go: generic stock or unique items."}
            )
        if accepted and attrs["route"] == GoodsReceiptLine.Route.UNIQUE and not attrs.get("units"):
            raise serializers.ValidationError(
                {"units": f"Enter details for the {accepted} unique item(s)."}
            )
        return attrs


class GoodsReceiptSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.material_type.name", read_only=True, default=None)
    wo_number = serializers.CharField(source="work_order.wo_number", read_only=True, default=None)
    po_number = serializers.CharField(source="purchase_order.po_number", read_only=True, default=None)
    received_by_name = serializers.CharField(source="received_by.get_full_name", read_only=True, default=None)
    lines = GoodsReceiptLineSerializer(many=True, read_only=True)

    class Meta:
        model = GoodsReceipt
        fields = [
            "id", "grn_number", "source", "work_order", "wo_number",
            "purchase_order", "po_number", "item", "item_name",
            "quantity", "reference", "received_by", "received_by_name",
            "notes", "lines", "created_at",
        ]
        # purchase_order receipts are created via POST /purchase-orders/{id}/receive/,
        # never through this legacy endpoint — hence read-only here.
        read_only_fields = ["id", "grn_number", "purchase_order", "received_by", "created_at"]
        # The model made item/quantity nullable for PO-level receipts; the
        # legacy endpoint still requires both so the old flow keeps working.
        extra_kwargs = {
            "item": {"required": True, "allow_null": False},
            "quantity": {"required": True, "allow_null": False, "min_value": 1},
        }


class IssuanceSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.material_type.name", read_only=True, default=None)
    site_name = serializers.CharField(source="issued_to_site.name", read_only=True, default=None)
    project_name = serializers.CharField(source="issued_to_project.name", read_only=True, default=None)
    # Accounts without a full name still have to answer "who authorised this".
    issued_by_name = serializers.SerializerMethodField()
    # Stock goes out to a project or to a named person; the log has to say which.
    issued_to_user_name = serializers.CharField(
        source="issued_to_user.get_full_name", read_only=True, default=None
    )

    class Meta:
        model = Issuance
        fields = [
            "id", "issue_number", "item", "item_name", "quantity",
            "issued_to_site", "site_name", "issued_to_work_order",
            "issued_to_user", "issued_to_user_name",
            "issued_to_project", "project_name", "bom_line",
            "issued_by", "issued_by_name", "reason", "notes", "created_at",
        ]
        read_only_fields = ["id", "issue_number", "issued_by", "created_at"]

    def get_issued_by_name(self, obj):
        user = obj.issued_by
        if user is None:
            return None
        return user.get_full_name() or user.username

    def validate(self, attrs):
        item = attrs.get("item")
        qty = attrs.get("quantity")
        if item is not None and qty is not None and qty > item.quantity:
            raise serializers.ValidationError(
                {"quantity": f"Only {item.quantity} unit(s) of {item} in stock."}
            )
        return attrs


class IssuanceRequestSerializer(serializers.ModelSerializer):
    """Material asked of the store, and what has been handed over so far."""

    what = serializers.CharField(read_only=True)
    item_sku = serializers.CharField(source="item.sku", read_only=True, default=None)
    item_name = serializers.CharField(
        source="item.material_type.name", read_only=True, default=None
    )
    unit_type_name = serializers.StringRelatedField(source="unit_type", read_only=True)
    # Quantities read with their unit of measure (piece, meter, box…).
    unit = serializers.SerializerMethodField()

    def get_unit(self, obj):
        if obj.unit_type_id:
            return obj.unit_type.unit or "piece"
        if obj.item_id and obj.item.material_type_id:
            return obj.item.material_type.unit or "piece"
        return "piece"
    project_name = serializers.CharField(source="project.name", read_only=True, default=None)
    asset_code = serializers.CharField(
        source="asset_component.device.asset_code", read_only=True, default=None
    )
    component_name = serializers.CharField(
        source="asset_component.name", read_only=True, default=None
    )
    maintenance_title = serializers.CharField(
        source="maintenance_schedule.title", read_only=True, default=None
    )
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    source_display = serializers.CharField(source="get_source_display", read_only=True)
    outstanding_quantity = serializers.IntegerField(read_only=True)
    available_quantity = serializers.IntegerField(read_only=True)
    requested_by_name = serializers.SerializerMethodField()
    issued_by_name = serializers.SerializerMethodField()
    # True while the goods are still being bought: the store cannot issue
    # until the PO is received and inspected into stock.
    awaiting_procurement = serializers.SerializerMethodField()
    po_number = serializers.SerializerMethodField()
    # For a unique item: every serial handed over, with where each unit stands now.
    issued_units = serializers.SerializerMethodField()

    def get_issued_units(self, obj):
        serials = obj.issued_serials or []
        if not serials:
            return []
        units = {
            u.serial_number: u
            for u in InventoryUnit.objects.filter(serial_number__in=serials).only(
                "serial_number", "unit_code", "status"
            )
        }
        out = []
        for sn in serials:
            unit = units.get(sn)
            out.append({
                "serial_number": sn,
                "unit_code": unit.unit_code if unit else None,
                "status": unit.status if unit else None,
                "status_display": unit.get_status_display() if unit else None,
            })
        return out

    def get_awaiting_procurement(self, obj):
        if not obj.awaiting_procurement:
            return False
        component = obj.asset_component
        line = component.purchase_order_item if component is not None else None
        # Still being bought until the order line has been received.
        return line is None or line.received_quantity < line.quantity

    def get_po_number(self, obj):
        component = obj.asset_component
        if component is None or component.purchase_order_item_id is None:
            return None
        return component.purchase_order_item.purchase_order.po_number

    class Meta:
        model = IssuanceRequest
        fields = [
            "id", "request_number", "what",
            "item", "item_sku", "item_name", "unit_type", "unit_type_name", "unit",
            "quantity_requested", "quantity_issued", "outstanding_quantity", "available_quantity",
            "source", "source_display", "purpose",
            "project", "project_name", "asset_component", "asset_code", "component_name",
            "maintenance_schedule", "maintenance_title",
            "requested_by", "requested_by_name", "issued_by", "issued_by_name",
            "received_by", "issued_serials", "issued_units", "last_issued_at", "awaiting_procurement", "po_number",
            "status", "status_display", "notes", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "request_number", "quantity_issued", "issued_by", "issued_serials", "issued_units",
            "last_issued_at", "received_by", "status", "requested_by", "created_at", "updated_at",
        ]

    def _name(self, user):
        if user is None:
            return None
        return user.get_full_name() or user.username

    def get_requested_by_name(self, obj):
        return self._name(obj.requested_by)

    def get_issued_by_name(self, obj):
        return self._name(obj.issued_by)

    def validate(self, attrs):
        def current(name):
            if name in attrs:
                return attrs[name]
            return getattr(self.instance, name, None)

        item, unit_type = current("item"), current("unit_type")
        if bool(item) == bool(unit_type):
            raise serializers.ValidationError(
                {"item": "Name either a stock item or a serialized product — one of the two."}
            )
        quantity = current("quantity_requested")
        if quantity is not None and quantity < 1:
            raise serializers.ValidationError({"quantity_requested": "Ask for at least one."})
        return attrs


class IssuanceRequestIssueSerializer(serializers.Serializer):
    """One hand-over against a request; the balance stays on the queue."""

    quantity = serializers.IntegerField(min_value=1)
    received_by = serializers.CharField(required=False, allow_blank=True, default="")
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class ReorderRequestSerializer(serializers.ModelSerializer):
    """A request to buy stock that fell to its reorder level."""

    item_sku = serializers.CharField(source="item.sku", read_only=True, default=None)
    unit_type_name = serializers.CharField(source="unit_type.name", read_only=True, default=None)
    name = serializers.CharField(read_only=True)
    code = serializers.CharField(read_only=True)
    kind = serializers.CharField(read_only=True)
    unit = serializers.CharField(read_only=True)
    on_hand = serializers.IntegerField(read_only=True)
    reorder_level = serializers.IntegerField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    po_number = serializers.CharField(source="purchase_order_item.purchase_order.po_number", read_only=True, default=None)
    requested_by_name = serializers.SerializerMethodField()

    class Meta:
        model = ReorderRequest
        fields = [
            "id", "item", "item_sku", "unit_type", "unit_type_name", "name", "code", "kind", "unit",
            "quantity", "reason", "status", "status_display", "purchase_order_item", "po_number",
            "on_hand", "reorder_level", "requested_by", "requested_by_name", "notes", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "status", "purchase_order_item", "requested_by", "created_at", "updated_at"]

    def get_requested_by_name(self, obj):
        user = obj.requested_by
        if user is None:
            return None
        return user.get_full_name() or user.username

    def validate(self, attrs):
        item = attrs.get("item")
        unit_type = attrs.get("unit_type")
        if bool(item) == bool(unit_type):
            raise serializers.ValidationError({"item": ["Name one stock item or one unique product."]})
        if (attrs.get("quantity") or 0) < 1:
            raise serializers.ValidationError({"quantity": ["Ask for at least one."]})
        if self.instance is None:
            live = ReorderRequest.objects.filter(status__in=(ReorderRequest.Status.OPEN, ReorderRequest.Status.ORDERED))
            live = live.filter(item=item) if item else live.filter(unit_type=unit_type)
            if live.exists():
                raise serializers.ValidationError({"detail": "A reorder request for this component is already open."})
        return attrs
