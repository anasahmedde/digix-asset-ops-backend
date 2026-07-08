from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.permissions import AdminManagerWriteElseRead

from .models import WorkOrder
from .pdf import build_work_order_pdf
from .serializers import (
    WorkOrderListSerializer,
    WorkOrderSerializer,
    WorkOrderTransitionSerializer,
)


def _spawn_project_if_needed(work_order: WorkOrder):
    """On approval, create the delivery Project sourced from this Work Order."""
    from apps.teams.models import Project

    if work_order.order_type == WorkOrder.OrderType.SUPPLY:
        return  # pure purchase — no installation project
    if Project.objects.filter(source_work_order=work_order).exists():
        return
    Project.objects.create(
        name=f"Install: {work_order.title}"[:300],
        description=work_order.description,
        client=work_order.client,
        site=work_order.site,
        source_work_order=work_order,
        status=Project.Status.PLANNING,
        start_date=work_order.order_date,
        target_date=work_order.expected_delivery,
    )


class WorkOrderViewSet(viewsets.ModelViewSet):
    queryset = (
        WorkOrder.objects.select_related(
            "supplier", "client", "site", "payment_terms", "terms_template",
            "created_by", "approved_by",
        )
        .prefetch_related("items", "items__asset_type", "items__device_model")
        .all()
    )
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["status", "order_type", "supplier", "client", "site"]
    search_fields = ["wo_number", "title", "description"]
    ordering_fields = ["created_at", "expected_delivery", "total_amount"]

    def get_serializer_class(self):
        if self.action == "list":
            return WorkOrderListSerializer
        return WorkOrderSerializer

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        work_order = self.get_object()
        ser = WorkOrderTransitionSerializer(data=request.data, context={"work_order": work_order})
        ser.is_valid(raise_exception=True)
        new_status = ser.validated_data["status"]
        now = timezone.now()

        update_fields = ["status", "updated_at"]
        work_order.status = new_status

        if new_status == WorkOrder.Status.APPROVED:
            work_order.approved_by = request.user
            work_order.approved_at = now
            update_fields += ["approved_by", "approved_at"]
        elif new_status == WorkOrder.Status.ISSUED:
            work_order.issued_at = now
            update_fields += ["issued_at"]

        work_order.save(update_fields=update_fields)

        if new_status == WorkOrder.Status.APPROVED:
            _spawn_project_if_needed(work_order)

        return Response(WorkOrderSerializer(work_order).data)

    @action(detail=True, methods=["get"], url_path="print")
    def print_pdf(self, request, pk=None):
        work_order = self.get_object()
        pdf_bytes = build_work_order_pdf(work_order)
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{work_order.wo_number}.pdf"'
        return response
