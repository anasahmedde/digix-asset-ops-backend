from django.db import transaction
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.permissions import WarehouseWriteElseRead

from .models import (
    GoodsReceipt,
    InventoryCategory,
    InventoryItem,
    Issuance,
    StockMovement,
)
from .serializers import (
    GoodsReceiptSerializer,
    InventoryCategorySerializer,
    InventoryItemSerializer,
    IssuanceSerializer,
    StockMovementSerializer,
)
from .services import apply_issuance_stock_out


class InventoryCategoryViewSet(viewsets.ModelViewSet):
    queryset = InventoryCategory.objects.all()
    serializer_class = InventoryCategorySerializer
    permission_classes = [IsAuthenticated, WarehouseWriteElseRead]
    filterset_fields = ["is_active"]
    search_fields = ["name"]


class InventoryItemViewSet(viewsets.ModelViewSet):
    # Coalesce so unpriced items sort as zero value instead of NULLs-first.
    queryset = (
        InventoryItem.objects.select_related("material_type", "category")
        .annotate(
            total_value=Coalesce(
                ExpressionWrapper(
                    F("quantity") * F("unit_cost"),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                ),
                Value(0),
                output_field=DecimalField(max_digits=14, decimal_places=2),
            )
        )
        .all()
    )
    serializer_class = InventoryItemSerializer
    permission_classes = [IsAuthenticated, WarehouseWriteElseRead]
    filterset_fields = ["location", "category", "material_type"]
    search_fields = ["sku", "material_type__name"]
    ordering_fields = ["quantity", "total_value", "material_type__name", "created_at"]

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """In-hand stock totals for dashboard widgets.

        ``total_value`` only counts items with a known unit_cost; ``unpriced_items``
        tells the client how many items are excluded from the valuation.
        """
        agg = InventoryItem.objects.aggregate(
            items=Count("id"),
            total_quantity=Sum("quantity"),
            total_value=Sum(
                ExpressionWrapper(
                    F("quantity") * F("unit_cost"),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                ),
                filter=Q(unit_cost__isnull=False),
            ),
            unpriced_items=Count("id", filter=Q(unit_cost__isnull=True)),
            low_stock=Count("id", filter=Q(quantity__lte=F("min_stock_level"))),
        )
        return Response({
            "items": agg["items"] or 0,
            "total_quantity": agg["total_quantity"] or 0,
            "total_value": agg["total_value"] or 0,
            "unpriced_items": agg["unpriced_items"] or 0,
            "low_stock": agg["low_stock"] or 0,
        })


class StockMovementViewSet(viewsets.ModelViewSet):
    queryset = StockMovement.objects.select_related("item", "performed_by").all()
    serializer_class = StockMovementSerializer
    permission_classes = [IsAuthenticated, WarehouseWriteElseRead]
    filterset_fields = ["item", "movement_type"]
    ordering_fields = ["created_at"]


class GoodsReceiptViewSet(viewsets.ModelViewSet):
    queryset = (
        GoodsReceipt.objects.select_related("item", "work_order", "purchase_order", "received_by")
        .prefetch_related("lines", "lines__po_item", "lines__inventory_item__material_type")
        .all()
    )
    serializer_class = GoodsReceiptSerializer
    permission_classes = [IsAuthenticated, WarehouseWriteElseRead]
    filterset_fields = ["item", "work_order", "purchase_order"]
    ordering_fields = ["created_at"]

    def perform_create(self, serializer):
        with transaction.atomic():
            receipt = serializer.save(received_by=self.request.user)
            item = InventoryItem.objects.select_for_update().get(pk=receipt.item_id)
            item.quantity += receipt.quantity
            item.save(update_fields=["quantity", "updated_at"])
            StockMovement.objects.create(
                item=item,
                movement_type=StockMovement.MovementType.IN,
                quantity=receipt.quantity,
                reference=receipt.grn_number,
                performed_by=self.request.user,
                notes=f"Goods receipt {receipt.grn_number}",
            )


class IssuanceViewSet(viewsets.ModelViewSet):
    queryset = Issuance.objects.select_related(
        "item", "issued_to_site", "issued_to_work_order", "issued_to_project", "issued_by"
    ).all()
    serializer_class = IssuanceSerializer
    permission_classes = [IsAuthenticated, WarehouseWriteElseRead]
    filterset_fields = ["item", "issued_to_site", "issued_to_work_order", "issued_to_project", "bom_line"]
    ordering_fields = ["created_at"]

    def perform_create(self, serializer):
        with transaction.atomic():
            issuance = serializer.save(issued_by=self.request.user)
            apply_issuance_stock_out(issuance, self.request.user)
