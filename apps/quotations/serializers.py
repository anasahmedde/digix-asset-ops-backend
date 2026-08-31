from rest_framework import serializers

from .models import Quotation, QuotationItem


class QuotationItemSerializer(serializers.ModelSerializer):
    """Nested under QuotationSerializer (mirrors the Wave-1 PO item pattern).

    ``id`` is writable so nested updates can upsert: rows carrying an existing
    id are updated in place, rows without one are created, and rows missing
    from the payload are deleted.
    """

    id = serializers.UUIDField(required=False)
    asset_type_name = serializers.CharField(source="asset_type.name", read_only=True, default=None)
    device_model_name = serializers.CharField(source="device_model.__str__", read_only=True, default=None)
    material_type_name = serializers.CharField(source="material_type.name", read_only=True, default=None)
    line_total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = QuotationItem
        fields = [
            "id", "asset_type", "asset_type_name", "device_model", "device_model_name",
            "material_type", "material_type_name", "description",
            "quantity", "unit_price", "line_total",
        ]
        read_only_fields = ["line_total"]

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Quantity must be a positive integer.")
        return value


class QuotationSerializer(serializers.ModelSerializer):
    items = QuotationItemSerializer(many=True, required=False)
    client_name = serializers.CharField(source="client.name", read_only=True, default=None)
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    created_by_name = serializers.CharField(source="created_by.get_full_name", read_only=True, default=None)
    spawned_project = serializers.SerializerMethodField()

    class Meta:
        model = Quotation
        fields = [
            "id", "quote_number", "title", "description",
            "status", "status_display",
            "client", "client_name", "site", "site_name",
            "currency", "valid_until", "total_amount", "notes",
            "items", "created_by", "created_by_name",
            "accepted_at", "spawned_project", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "quote_number", "status", "total_amount", "created_by",
            "accepted_at", "created_at", "updated_at",
        ]

    def get_spawned_project(self, obj):
        project = obj.spawned_projects.first()
        return str(project.pk) if project else None

    def create(self, validated_data):
        items = validated_data.pop("items", [])
        quotation = Quotation.objects.create(**validated_data)
        for item in items:
            item.pop("id", None)
            QuotationItem.objects.create(quotation=quotation, **item)
        quotation.recalc_total()
        return quotation

    def update(self, instance, validated_data):
        items = validated_data.pop("items", None)

        if items is not None and instance.status != Quotation.Status.DRAFT:
            raise serializers.ValidationError({
                "items": (
                    f"Line items cannot be modified while the quotation is "
                    f"'{instance.get_status_display()}'. Items are only editable in Draft."
                )
            })

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if items is not None:
            # Upsert: update rows carrying an existing id, create rows
            # without one, delete rows missing from the payload.
            existing = {item.pk: item for item in instance.items.all()}
            seen_ids = set()
            for item_data in items:
                item_id = item_data.pop("id", None)
                if item_id is not None:
                    item = existing.get(item_id)
                    if item is None:
                        raise serializers.ValidationError({
                            "items": f"Item '{item_id}' does not belong to this quotation."
                        })
                    for attr, value in item_data.items():
                        setattr(item, attr, value)
                    item.save()
                    seen_ids.add(item.pk)
                else:
                    item = QuotationItem.objects.create(quotation=instance, **item_data)
                    seen_ids.add(item.pk)
            for item_id, item in existing.items():
                if item_id not in seen_ids:
                    item.delete()
            # The viewset prefetches "items"; drop the stale cache so
            # recalc_total() and the rendered response see the new rows.
            instance._prefetched_objects_cache = {}

        instance.recalc_total()
        return instance


class QuotationTransitionSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=Quotation.Status.choices)
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate_status(self, value):
        quotation = self.context["quotation"]
        if not quotation.can_transition_to(value):
            raise serializers.ValidationError(
                f"Cannot move from '{quotation.get_status_display()}' to '{value}'."
            )
        return value
