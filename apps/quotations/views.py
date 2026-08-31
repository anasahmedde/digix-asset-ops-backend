from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.permissions import AdminManagerWriteElseRead

from .models import Quotation
from .pdf import build_quotation_pdf
from .serializers import QuotationSerializer, QuotationTransitionSerializer


def _spawn_project_if_needed(quotation: Quotation):
    """On acceptance, create the delivery Project sourced from this quotation
    and copy its items onto the project's BOM (mirrors the work-order spawn)."""
    from apps.teams.models import Project, ProjectBOMLine

    if Project.objects.filter(source_quotation=quotation).exists():
        return
    project = Project.objects.create(
        name=f"Project: {quotation.title}"[:300],
        description=quotation.description,
        client=quotation.client,
        site=quotation.site,
        source_quotation=quotation,
        phase=Project.Phase.ORDER_CONFIRMATION,
        status=Project.Status.PLANNING,
    )
    for item in quotation.items.all():
        ProjectBOMLine.objects.create(
            project=project,
            asset_type=item.asset_type,
            device_model=item.device_model,
            material_type=item.material_type,
            description=item.description,
            quantity=item.quantity,
            unit_price=item.unit_price,
            source_quotation_item=item,
        )


class QuotationViewSet(viewsets.ModelViewSet):
    queryset = (
        Quotation.objects.select_related("client", "site", "created_by")
        .prefetch_related(
            "items", "items__asset_type", "items__device_model", "items__material_type",
            "spawned_projects",
        )
        .all()
    )
    serializer_class = QuotationSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["status", "client", "site", "currency"]
    search_fields = ["quote_number", "title", "description"]
    ordering_fields = ["created_at", "valid_until", "total_amount"]

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        quotation = self.get_object()
        ser = QuotationTransitionSerializer(data=request.data, context={"quotation": quotation})
        ser.is_valid(raise_exception=True)
        new_status = ser.validated_data["status"]

        update_fields = ["status", "updated_at"]
        quotation.status = new_status

        if new_status == Quotation.Status.ACCEPTED:
            quotation.accepted_at = timezone.now()
            update_fields += ["accepted_at"]

        # Acceptance and the project/BOM spawn must land together — a
        # failure mid-copy must not leave an accepted quote with half a BOM.
        with transaction.atomic():
            quotation.save(update_fields=update_fields)
            if new_status == Quotation.Status.ACCEPTED:
                _spawn_project_if_needed(quotation)

        if new_status == Quotation.Status.ACCEPTED:
            # get_object() prefetched spawned_projects before the spawn —
            # drop the stale cache so the response carries the new project.
            quotation._prefetched_objects_cache = {}

        return Response(QuotationSerializer(quotation).data)

    @action(detail=True, methods=["get"], url_path="print")
    def print_pdf(self, request, pk=None):
        quotation = self.get_object()
        pdf_bytes = build_quotation_pdf(quotation)
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{quotation.quote_number}.pdf"'
        return response
