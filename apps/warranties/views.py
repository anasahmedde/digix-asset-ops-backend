from dateutil.relativedelta import relativedelta
from django.utils import timezone
from rest_framework import status as http_status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.exports import EXPORT_MAX_ROWS, export_params, log_export, xlsx_response
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
            qs = qs.filter(warranty_type__in=CLIENT_TYPES)
        elif role in SUPPLIER_SIDE_ROLES:
            qs = qs.filter(warranty_type__in=SUPPLIER_TYPES)
        # ?side=client|supplier — lets dual-side roles narrow to one side of
        # the ledger. Handled here so list AND export share it; unknown
        # values are ignored.
        side = self.request.query_params.get("side")
        if side == "client":
            qs = qs.filter(warranty_type__in=CLIENT_TYPES)
        elif side == "supplier":
            qs = qs.filter(warranty_type__in=SUPPLIER_TYPES)
        return qs

    @action(detail=False, methods=["get"], url_path="export")
    def export(self, request):
        """Excel export of warranties — role-scoped and filter-aware (XC-01)."""
        qs = self.filter_queryset(self.get_queryset())[:EXPORT_MAX_ROWS]
        columns = [
            "Asset Code", "Component", "Warranty Type", "Status",
            "Start Date", "End Date", "Months", "Supplier", "Reference #",
        ]
        rows = []
        for w in qs:
            rows.append([
                w.device.asset_code if w.device_id else "",
                w.component.name if w.component_id else "",
                w.get_warranty_type_display(),
                w.get_status_display(),
                w.start_date,
                w.end_date,
                w.months,
                w.supplier.name if w.supplier_id else "",
                w.reference_number,
            ])
        log_export(request.user, "warranty", len(rows), export_params(request))
        return xlsx_response("warranties", "Warranties", columns, rows)

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

    @action(detail=True, methods=["post"])
    def extend(self, request, pk=None):
        """Push a warranty's expiry later — an extension the vendor granted.

        The warranty keeps its identity and its start date; only the end moves.
        The change is written onto the warranty and journalled on the asset, so
        the history of extensions is never lost behind the current date.
        """
        from django.utils.dateparse import parse_date

        from apps.assets.models import DeviceLifecycleEvent

        warranty = self.get_object()
        if warranty.warranty_type == Warranty.WarrantyType.CLIENT:
            return Response(
                {"detail": "Client warranties are reissued, not extended — use Reissue."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        if warranty.status in (Warranty.Status.VOID, Warranty.Status.REISSUED):
            return Response(
                {"detail": f"A {warranty.get_status_display().lower()} warranty cannot be extended."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        raw_end, raw_months = request.data.get("end_date"), request.data.get("months")
        if raw_end:
            new_end = parse_date(str(raw_end))
            if new_end is None:
                return Response({"end_date": ["Use a real date."]}, status=http_status.HTTP_400_BAD_REQUEST)
        elif raw_months not in (None, ""):
            try:
                months = int(raw_months)
            except (TypeError, ValueError):
                return Response({"months": ["Must be a whole number."]}, status=http_status.HTTP_400_BAD_REQUEST)
            if months < 1:
                return Response({"months": ["Add at least one month."]}, status=http_status.HTTP_400_BAD_REQUEST)
            new_end = warranty.end_date + relativedelta(months=months)
        else:
            return Response(
                {"detail": "Give the new expiry date, or how many months to add."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        if new_end <= warranty.end_date:
            return Response(
                {"end_date": [f"An extension has to move the expiry later than {warranty.end_date:%d %b %Y}."]},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        old_end = warranty.end_date
        reference = (request.data.get("reference_number") or "").strip()
        notes = (request.data.get("notes") or "").strip()
        who = request.user.get_full_name() or request.user.username

        warranty.end_date = new_end
        delta = relativedelta(new_end, warranty.start_date)
        warranty.months = max(1, delta.years * 12 + delta.months + (1 if delta.days else 0))
        if warranty.status == Warranty.Status.EXPIRED and new_end > timezone.now().date():
            warranty.status = Warranty.Status.ACTIVE
        entry = f"Extended {old_end:%d %b %Y} → {new_end:%d %b %Y} by {who}"
        if reference:
            entry += f" — ref {reference}"
        if notes:
            entry += f" — {notes}"
        warranty.notes = f"{warranty.notes}\n{entry}" if warranty.notes else entry
        warranty.save(update_fields=["end_date", "months", "status", "notes", "updated_at"])

        DeviceLifecycleEvent.objects.create(
            device=warranty.device,
            event_type=DeviceLifecycleEvent.EventType.NOTE,
            description=f"{warranty.get_warranty_type_display()} warranty extended to {new_end:%d %b %Y}",
            performed_by=request.user,
            metadata={"warranty": str(warranty.pk), "from": str(old_end), "to": str(new_end)},
        )
        return Response(WarrantySerializer(warranty).data)
