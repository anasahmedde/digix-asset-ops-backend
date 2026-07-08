from django.db import transaction
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

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


class InventoryCategoryViewSet(viewsets.ModelViewSet):
    queryset = InventoryCategory.objects.all()
    serializer_class = InventoryCategorySerializer
    permission_classes = [IsAuthenticated, WarehouseWriteElseRead]
    filterset_fields = ["is_active"]
    search_fields = ["name"]


class InventoryItemViewSet(viewsets.ModelViewSet):
    queryset = InventoryItem.objects.select_related("material_type", "category").all()
    serializer_class = InventoryItemSerializer
    permission_classes = [IsAuthenticated, WarehouseWriteElseRead]
    filterset_fields = ["location", "category", "material_type"]
    search_fields = ["sku", "material_type__name"]
    ordering_fields = ["quantity", "created_at"]


class StockMovementViewSet(viewsets.ModelViewSet):
    queryset = StockMovement.objects.select_related("item", "performed_by").all()
    serializer_class = StockMovementSerializer
    permission_classes = [IsAuthenticated, WarehouseWriteElseRead]
    filterset_fields = ["item", "movement_type"]
    ordering_fields = ["created_at"]


class GoodsReceiptViewSet(viewsets.ModelViewSet):
    queryset = GoodsReceipt.objects.select_related("item", "work_order", "received_by").all()
    serializer_class = GoodsReceiptSerializer
    permission_classes = [IsAuthenticated, WarehouseWriteElseRead]
    filterset_fields = ["item", "work_order"]
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
        "item", "issued_to_site", "issued_to_work_order", "issued_by"
    ).all()
    serializer_class = IssuanceSerializer
    permission_classes = [IsAuthenticated, WarehouseWriteElseRead]
    filterset_fields = ["item", "issued_to_site", "issued_to_work_order"]
    ordering_fields = ["created_at"]

    def perform_create(self, serializer):
        with transaction.atomic():
            issuance = serializer.save(issued_by=self.request.user)
            item = InventoryItem.objects.select_for_update().get(pk=issuance.item_id)
            item.quantity -= issuance.quantity
            item.save(update_fields=["quantity", "updated_at"])
            StockMovement.objects.create(
                item=item,
                movement_type=StockMovement.MovementType.OUT,
                quantity=issuance.quantity,
                reference=issuance.issue_number,
                performed_by=self.request.user,
                notes=issuance.reason or f"Issued {issuance.issue_number}",
            )
