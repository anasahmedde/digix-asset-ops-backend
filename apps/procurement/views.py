from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from common.permissions import FinanceWriteElseRead

from .models import PurchaseOrder, PurchaseOrderItem
from .serializers import PurchaseOrderItemSerializer, PurchaseOrderSerializer


class PurchaseOrderViewSet(viewsets.ModelViewSet):
    queryset = (
        PurchaseOrder.objects.select_related("supplier", "ordered_by", "approved_by")
        .prefetch_related("items")
        .all()
    )
    serializer_class = PurchaseOrderSerializer
    permission_classes = [IsAuthenticated, FinanceWriteElseRead]
    filterset_fields = ["status", "supplier"]
    search_fields = ["po_number"]
    ordering_fields = ["created_at", "order_date", "total_amount"]


class PurchaseOrderItemViewSet(viewsets.ModelViewSet):
    queryset = PurchaseOrderItem.objects.select_related("purchase_order").all()
    serializer_class = PurchaseOrderItemSerializer
    permission_classes = [IsAuthenticated, FinanceWriteElseRead]
    filterset_fields = ["purchase_order"]
