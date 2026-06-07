from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from common.permissions import WarehouseWriteElseRead

from .models import InventoryItem, StockMovement
from .serializers import InventoryItemSerializer, StockMovementSerializer


class InventoryItemViewSet(viewsets.ModelViewSet):
    queryset = InventoryItem.objects.select_related(
        "material_type", "site"
    ).all()
    serializer_class = InventoryItemSerializer
    permission_classes = [IsAuthenticated, WarehouseWriteElseRead]
    filterset_fields = ["location", "site", "material_type"]
    search_fields = ["sku", "material_type__name"]
    ordering_fields = ["quantity", "created_at"]


class StockMovementViewSet(viewsets.ModelViewSet):
    queryset = StockMovement.objects.select_related(
        "item", "performed_by"
    ).all()
    serializer_class = StockMovementSerializer
    permission_classes = [IsAuthenticated, WarehouseWriteElseRead]
    filterset_fields = ["item", "movement_type"]
    ordering_fields = ["created_at"]
