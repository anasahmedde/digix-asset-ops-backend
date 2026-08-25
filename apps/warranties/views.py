from dateutil.relativedelta import relativedelta
from django.utils import timezone
from rest_framework import status as http_status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.permissions import AdminManagerWriteElseRead

from .models import Warranty
from .serializers import WarrantySerializer

REISSUE_TERMS = (3, 6, 12)

# Client-facing warranties belong to marketing; supplier-facing ones to
# operations/production. Admin-level roles see both sides.
CLIENT_TYPES = ("client",)
SUPPLIER_TYPES = ("manufacturer", "extended", "supplier")
CLIENT_SIDE_ROLES = ("marketing", "marketing_head", "client_viewer")
SUPPLIER_SIDE_ROLES = ("ops_manager", "supervisor", "technician", "warehouse")


class WarrantyViewSet(viewsets.ModelViewSet):
    queryset = Warranty.objects.select_related("device", "supplier", "component").all()
    serializer_class = WarrantySerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["status", "warranty_type", "device", "supplier"]
    search_fields = ["reference_number", "coverage_details", "device__asset_code", "device__display_name"]
    ordering_fields = ["end_date", "start_date"]

    def get_queryset(self):
        qs = super().get_queryset()
        role = getattr(self.request.user, "role", None)
        if role in CLIENT_SIDE_ROLES:
            return qs.filter(warranty_type__in=CLIENT_TYPES)
        if role in SUPPLIER_SIDE_ROLES:
            return qs.filter(warranty_type__in=SUPPLIER_TYPES)
        return qs

    @action(detail=True, methods=["post"])
    def reissue(self, request, pk=None):
        """Reissue a completed warranty as a new client warranty (3/6/12 months).

        The original is marked ``reissued``; the replacement starts today and
        links back via ``reissued_from``.
        """
        original = self.get_object()
        if original.status not in (Warranty.Status.EXPIRED, Warranty.Status.ACTIVE):
            return Response(
                {"detail": "Only active or completed warranties can be reissued."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        try:
            months = int(request.data.get("months", 0))
        except (TypeError, ValueError):
            months = 0
        if months not in REISSUE_TERMS:
            return Response(
                {"months": [f"Must be one of: {', '.join(map(str, REISSUE_TERMS))}."]},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        today = timezone.now().date()
        replacement = Warranty.objects.create(
            device=original.device,
            supplier=original.supplier,
            warranty_type=Warranty.WarrantyType.CLIENT,
            status=Warranty.Status.ACTIVE,
            start_date=today,
            end_date=today + relativedelta(months=months),
            months=months,
            reissued_from=original,
            coverage_details=original.coverage_details,
            notes=f"Reissued from {original.reference_number or original.pk} for {months} months.",
        )
        original.status = Warranty.Status.REISSUED
        original.save(update_fields=["status", "updated_at"])
        return Response(WarrantySerializer(replacement).data, status=http_status.HTTP_201_CREATED)
