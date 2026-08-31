from rest_framework import serializers

from apps.suppliers.models import Supplier
from apps.teams.models import Project

from .models import PurchaseOrder, PurchaseOrderItem


class PurchaseOrderItemSerializer(serializers.ModelSerializer):
    """Nested under PurchaseOrderSerializer (mirrors WorkOrderItemSerializer).

    ``id`` is writable so nested updates can upsert: rows carrying an existing
    id are updated in place (preserving received_quantity), rows without one
    are created, and rows missing from the payload are deleted.
    """

    id = serializers.UUIDField(required=False)
    asset_type_name = serializers.CharField(source="asset_type.name", read_only=True, default=None)
    device_model_name = serializers.CharField(source="device_model.__str__", read_only=True, default=None)
    material_type_name = serializers.CharField(source="material_type.name", read_only=True, default=None)
    line_total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = PurchaseOrderItem
        fields = [
            "id", "asset_type", "asset_type_name", "device_model", "device_model_name",
            "material_type", "material_type_name", "bom_line", "description",
            "quantity", "unit_price", "received_quantity", "line_total",
        ]
        # received_quantity is owned by goods receiving — never writable via the API.
        read_only_fields = ["line_total", "received_quantity"]


class PurchaseOrderItemDetailSerializer(PurchaseOrderItemSerializer):
    """Standalone item endpoint — includes the parent purchase order."""

    id = serializers.UUIDField(read_only=True)

    class Meta(PurchaseOrderItemSerializer.Meta):
        fields = PurchaseOrderItemSerializer.Meta.fields + ["purchase_order"]


class PurchaseOrderSerializer(serializers.ModelSerializer):
    items = PurchaseOrderItemSerializer(many=True, required=False)
    supplier_name = serializers.CharField(source="supplier.name", read_only=True, default=None)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    ordered_by_name = serializers.CharField(source="ordered_by.get_full_name", read_only=True, default=None)
    approved_by_name = serializers.CharField(source="approved_by.get_full_name", read_only=True, default=None)

    class Meta:
        model = PurchaseOrder
        fields = [
            "id", "po_number", "supplier", "supplier_name",
            "status", "status_display", "currency", "order_date", "expected_delivery",
            "total_amount", "notes",
            "ordered_by", "ordered_by_name", "approved_by", "approved_by_name",
            "items", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "po_number", "status", "total_amount", "ordered_by", "approved_by",
            "created_at", "updated_at",
        ]

    # Line items may only be modified while the PO is still editable.
    ITEM_WRITE_STATUSES = (PurchaseOrder.Status.DRAFT, PurchaseOrder.Status.PENDING_APPROVAL)

    def create(self, validated_data):
        items = validated_data.pop("items", [])
        purchase_order = PurchaseOrder.objects.create(**validated_data)
        for item in items:
            item.pop("id", None)
            PurchaseOrderItem.objects.create(purchase_order=purchase_order, **item)
        purchase_order.recalc_total()
        return purchase_order

    def update(self, instance, validated_data):
        items = validated_data.pop("items", None)

        if items is not None and instance.status not in self.ITEM_WRITE_STATUSES:
            raise serializers.ValidationError({
                "items": (
                    f"Line items cannot be modified while the purchase order is "
                    f"'{instance.get_status_display()}'. Items are only editable in "
                    f"Draft or Pending Approval."
                )
            })

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if items is not None:
            # Upsert: update rows carrying an existing id (preserving
            # received_quantity), create rows without one, delete rows
            # missing from the payload.
            existing = {item.pk: item for item in instance.items.all()}
            seen_ids = set()
            for item_data in items:
                item_id = item_data.pop("id", None)
                if item_id is not None:
                    item = existing.get(item_id)
                    if item is None:
                        raise serializers.ValidationError({
                            "items": f"Item '{item_id}' does not belong to this purchase order."
                        })
                    for attr, value in item_data.items():
                        setattr(item, attr, value)
                    item.save()
                    seen_ids.add(item.pk)
                else:
                    item = PurchaseOrderItem.objects.create(purchase_order=instance, **item_data)
                    seen_ids.add(item.pk)
            for item_id, item in existing.items():
                if item_id not in seen_ids:
                    item.delete()
            # The viewset prefetches "items"; drop the stale cache so
            # recalc_total() and the rendered response see the new rows.
            instance._prefetched_objects_cache = {}

        instance.recalc_total()
        return instance


class PurchaseOrderFromShortageSerializer(serializers.Serializer):
    """Input for POST /purchase-orders/from-shortage/ — raise a draft PO
    covering a project's unallocated BOM quantities."""

    project = serializers.PrimaryKeyRelatedField(queryset=Project.objects.all())
    supplier = serializers.PrimaryKeyRelatedField(queryset=Supplier.objects.all())
    currency = serializers.ChoiceField(choices=PurchaseOrder.Currency.choices, required=False)
    line_ids = serializers.ListField(child=serializers.UUIDField(), required=False, allow_empty=False)

    def validate(self, attrs):
        project = attrs["project"]
        lines = project.bom_lines.prefetch_related("allocations").all()
        line_ids = attrs.get("line_ids")
        if line_ids is not None:
            by_id = {line.pk: line for line in lines}
            missing = [str(pk) for pk in line_ids if pk not in by_id]
            if missing:
                raise serializers.ValidationError({
                    "line_ids": f"BOM lines not on this project: {', '.join(missing)}."
                })
            lines = [by_id[pk] for pk in line_ids]
        shortage_lines = [line for line in lines if line.shortage > 0]
        if not shortage_lines:
            raise serializers.ValidationError({
                "line_ids": "Nothing to order — no BOM lines with a shortage."
            })
        attrs["shortage_lines"] = shortage_lines
        return attrs


class PurchaseOrderReceiveLineSerializer(serializers.Serializer):
    """One received line in POST /purchase-orders/{id}/receive/."""

    po_item = serializers.UUIDField()
    quantity = serializers.IntegerField(min_value=1)
    batch_number = serializers.CharField(required=False, allow_blank=True, max_length=100)
    serial_numbers = serializers.ListField(
        child=serializers.CharField(max_length=200), required=False, default=list
    )


class PurchaseOrderReceiveSerializer(serializers.Serializer):
    """Input for POST /purchase-orders/{id}/receive/ — record a goods receipt
    against the PO. Business rules (status, quantities, serial uniqueness)
    are enforced by services.receive_against_po."""

    reference = serializers.CharField(required=False, allow_blank=True, max_length=200, default="")
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    lines = PurchaseOrderReceiveLineSerializer(many=True, allow_empty=False)


class PurchaseOrderTransitionSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=PurchaseOrder.Status.choices)
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate_status(self, value):
        purchase_order = self.context["purchase_order"]
        if not purchase_order.can_transition_to(value):
            raise serializers.ValidationError(
                f"Cannot move from '{purchase_order.get_status_display()}' to '{value}'."
            )
        return value
