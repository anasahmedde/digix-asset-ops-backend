from rest_framework import serializers

from .models import WorkOrder, WorkOrderItem


class WorkOrderItemSerializer(serializers.ModelSerializer):
    asset_type_name = serializers.CharField(source="asset_type.name", read_only=True, default=None)
    device_model_name = serializers.CharField(source="device_model.__str__", read_only=True, default=None)
    line_total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = WorkOrderItem
        fields = [
            "id", "asset_type", "asset_type_name", "device_model", "device_model_name",
            "description", "quantity", "unit_price", "received_quantity", "line_total",
        ]
        read_only_fields = ["id", "line_total"]


class WorkOrderListSerializer(serializers.ModelSerializer):
    supplier_name = serializers.CharField(source="supplier.name", read_only=True)
    client_name = serializers.CharField(source="client.name", read_only=True, default=None)
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    order_type_display = serializers.CharField(source="get_order_type_display", read_only=True)

    class Meta:
        model = WorkOrder
        fields = [
            "id", "wo_number", "title", "order_type", "order_type_display",
            "status", "status_display", "supplier", "supplier_name",
            "client_name", "site_name", "currency", "total_amount",
            "expected_delivery", "created_at",
        ]


class WorkOrderSerializer(serializers.ModelSerializer):
    items = WorkOrderItemSerializer(many=True, required=False)
    supplier_name = serializers.CharField(source="supplier.name", read_only=True)
    client_name = serializers.CharField(source="client.name", read_only=True, default=None)
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)
    payment_terms_name = serializers.CharField(source="payment_terms.name", read_only=True, default=None)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    order_type_display = serializers.CharField(source="get_order_type_display", read_only=True)
    created_by_name = serializers.CharField(source="created_by.get_full_name", read_only=True, default=None)
    approved_by_name = serializers.CharField(source="approved_by.get_full_name", read_only=True, default=None)

    class Meta:
        model = WorkOrder
        fields = [
            "id", "wo_number", "title", "description", "order_type", "order_type_display",
            "status", "status_display",
            "supplier", "supplier_name", "client", "client_name", "site", "site_name",
            "payment_terms", "payment_terms_name", "terms_template", "terms_conditions",
            "safety_instructions", "warranty_months",
            "currency", "order_date", "expected_delivery", "total_amount", "notes",
            "items", "created_by", "created_by_name", "approved_by", "approved_by_name",
            "approved_at", "issued_at", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "wo_number", "total_amount", "created_by", "approved_by",
            "approved_at", "issued_at", "created_at", "updated_at",
        ]

    def create(self, validated_data):
        items = validated_data.pop("items", [])
        work_order = WorkOrder.objects.create(**validated_data)
        for item in items:
            WorkOrderItem.objects.create(work_order=work_order, **item)
        work_order.recalc_total()
        return work_order

    def update(self, instance, validated_data):
        items = validated_data.pop("items", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if items is not None:
            instance.items.all().delete()
            for item in items:
                WorkOrderItem.objects.create(work_order=instance, **item)
        instance.recalc_total()
        return instance


class WorkOrderTransitionSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=WorkOrder.Status.choices)
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate_status(self, value):
        work_order = self.context["work_order"]
        if not work_order.can_transition_to(value):
            raise serializers.ValidationError(
                f"Cannot move from '{work_order.get_status_display()}' to '{value}'."
            )
        return value
