from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.permissions import FinanceWriteElseRead

from .models import PurchaseOrder, PurchaseOrderItem
from .serializers import (
    PurchaseOrderItemDetailSerializer,
    PurchaseOrderSerializer,
    PurchaseOrderTransitionSerializer,
)


class PurchaseOrderViewSet(viewsets.ModelViewSet):
    queryset = (
        PurchaseOrder.objects.select_related("supplier", "ordered_by", "approved_by")
        .prefetch_related(
            "items", "items__asset_type", "items__device_model", "items__material_type"
        )
        .all()
    )
    serializer_class = PurchaseOrderSerializer
    permission_classes = [IsAuthenticated, FinanceWriteElseRead]
    filterset_fields = ["status", "supplier"]
    search_fields = ["po_number"]
    ordering_fields = ["created_at", "order_date", "total_amount"]

    def perform_create(self, serializer):
        serializer.save(ordered_by=self.request.user)

    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        purchase_order = self.get_object()
        ser = PurchaseOrderTransitionSerializer(
            data=request.data, context={"purchase_order": purchase_order}
        )
        ser.is_valid(raise_exception=True)
        new_status = ser.validated_data["status"]
        notes = ser.validated_data.get("notes", "").strip()

        update_fields = ["status", "updated_at"]
        old_status_display = purchase_order.get_status_display()
        purchase_order.status = new_status

        if new_status == PurchaseOrder.Status.APPROVED:
            purchase_order.approved_by = request.user
            update_fields += ["approved_by"]

        if notes:
            stamp = timezone.localtime().strftime("%Y-%m-%d %H:%M")
            who = request.user.get_full_name() or request.user.username
            line = (
                f"[{stamp}] {who}: "
                f"{old_status_display} → {purchase_order.get_status_display()} — {notes}"
            )
            purchase_order.notes = f"{purchase_order.notes}\n{line}" if purchase_order.notes else line
            update_fields += ["notes"]

        purchase_order.save(update_fields=update_fields)

        return Response(PurchaseOrderSerializer(purchase_order).data)


class PurchaseOrderItemViewSet(viewsets.ModelViewSet):
    queryset = PurchaseOrderItem.objects.select_related(
        "purchase_order", "asset_type", "device_model", "material_type"
    ).all()
    serializer_class = PurchaseOrderItemDetailSerializer
    permission_classes = [IsAuthenticated, FinanceWriteElseRead]
    filterset_fields = ["purchase_order"]

    def perform_create(self, serializer):
        item = serializer.save()
        item.purchase_order.recalc_total()

    def perform_update(self, serializer):
        old_parent = serializer.instance.purchase_order
        item = serializer.save()
        item.purchase_order.recalc_total()
        if item.purchase_order.pk != old_parent.pk:
            old_parent.recalc_total()

    def perform_destroy(self, instance):
        purchase_order = instance.purchase_order
        instance.delete()
        purchase_order.recalc_total()
