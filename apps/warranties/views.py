from dateutil.relativedelta import relativedelta
from django.utils import timezone
from rest_framework import status as http_status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.exports import EXPORT_MAX_ROWS, export_params, log_export, xlsx_response
from apps.notifications import service as notices
from common.permissions import CapabilityGate

from .models import Warranty, WarrantyClaim
from .serializers import WarrantyClaimSerializer, WarrantySerializer

REISSUE_TERMS = (3, 6, 12)

# Client-facing warranties belong to marketing; supplier-facing ones to
# operations/production. Admin-level roles see both sides.
CLIENT_TYPES = ("client",)
SUPPLIER_TYPES = ("manufacturer", "extended", "supplier")
CLIENT_SIDE_ROLES = ("marketing", "marketing_head", "client_viewer")
SUPPLIER_SIDE_ROLES = ("ops_manager", "supervisor", "technician", "warehouse")


class WarrantyViewSet(viewsets.ModelViewSet):
    # Warranty cover is read across the business — the field needs to
    # know what an asset is covered for. Writing is the gated half.
    queryset = Warranty.objects.select_related(
        "device", "device__current_site", "device__assigned_client", "supplier", "component",
    ).all()
    serializer_class = WarrantySerializer
    permission_classes = [IsAuthenticated, CapabilityGate]
    read_capability = "view_warranties"
    write_capability = "manage_warranties"
    filterset_fields = ["status", "warranty_type", "device", "supplier"]
    search_fields = [
        "reference_number", "vendor_reference", "coverage_details", "device__asset_code", "device__display_name",
    ]
    ordering_fields = ["end_date", "start_date"]

    def get_queryset(self):
        from .services import expire_lapsed

        # Lapsed cover reads as completed the moment it lapses, not when the
        # background job next gets round to it.
        expire_lapsed()
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
        """Push a warranty's expiry later: the client's cover or the vendor's.

        The warranty keeps its identity and its start date; only the end moves.
        The change is written onto the warranty and journalled on the asset, so
        the history of extensions is never lost behind the current date.
        """
        from apps.assets.models import DeviceLifecycleEvent

        from .services import extended_expiry, extension_entry, term_months

        warranty = self.get_object()
        if warranty.status in (Warranty.Status.VOID, Warranty.Status.REISSUED):
            return Response(
                {"detail": f"A {warranty.get_status_display().lower()} warranty cannot be extended."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        old_end = warranty.end_date
        new_end = extended_expiry(old_end, request.data)

        warranty.end_date = new_end
        warranty.months = term_months(warranty.start_date, new_end)
        if warranty.status == Warranty.Status.EXPIRED and new_end > timezone.now().date():
            warranty.status = Warranty.Status.ACTIVE
        entry = extension_entry(old_end, new_end, request.user, request.data)
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


class WarrantyClaimViewSet(viewsets.ModelViewSet):
    """Claims on vendor and component warranties: raise, send, decide, settle."""

    queryset = WarrantyClaim.objects.select_related(
        "warranty", "inventory_unit", "inventory_unit__unit_type", "device", "supplier", "raised_by",
    ).all()
    serializer_class = WarrantyClaimSerializer
    permission_classes = [IsAuthenticated, CapabilityGate]
    read_capability = "view_warranties"
    write_capability = "decide_claim"
    action_capabilities = {"create": "raise_claim", "transition": "decide_claim"}
    filterset_fields = ["status", "supplier", "device", "warranty", "inventory_unit"]
    search_fields = ["claim_number", "vendor_reference", "fault", "device__asset_code", "inventory_unit__serial_number"]
    ordering_fields = ["created_at", "failure_date", "status", "claim_number"]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def perform_create(self, serializer):
        claim = serializer.save(raised_by=self.request.user)
        self._journal(claim, f"Raised: {claim.fault}")
        notices.ask(
            "decide_claim", exclude=[self.request.user], kind="request_raised",
            title=f"Warranty claim raised: {claim.claim_number}",
            message=f"{claim.fault} · {claim.supplier.name if claim.supplier_id else 'vendor'}",
            link="/warranties?tab=claims", ref=f"claim:{claim.pk}", data={"claim": str(claim.pk)},
        )

    def _journal(self, claim, line):
        from apps.assets.models import DeviceLifecycleEvent

        who = self.request.user.get_full_name() or self.request.user.username
        stamp = timezone.localtime().strftime("%Y-%m-%d %H:%M")
        entry = f"[{stamp}] {line} - {who}"
        claim.history = f"{claim.history}\n{entry}" if claim.history else entry
        claim.save(update_fields=["history", "updated_at"])
        if claim.device_id:
            DeviceLifecycleEvent.objects.create(
                device=claim.device,
                event_type=DeviceLifecycleEvent.EventType.NOTE,
                description=f"Warranty claim {claim.claim_number}: {line}",
                performed_by=self.request.user,
                metadata={"warranty_claim": str(claim.pk)},
            )

    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        """Move a claim on: send it, record the vendor's decision, settle it.

        ``{status, notes?, vendor_reference?, resolution?, recovered_amount?}``
        """
        from decimal import Decimal, InvalidOperation

        claim = self.get_object()
        new = request.data.get("status")
        notes = (request.data.get("notes") or "").strip()
        if new not in WarrantyClaim.NEXT.get(claim.status, ()):
            return Response(
                {"status": [f"A claim that is {claim.get_status_display().lower()} cannot move to that step."]},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        now = timezone.now()
        S = WarrantyClaim.Status
        if new == S.SUBMITTED:
            claim.vendor_reference = (request.data.get("vendor_reference") or claim.vendor_reference).strip()[:200]
            claim.submitted_at = now
        elif new in (S.APPROVED, S.REJECTED):
            if new == S.REJECTED and not notes:
                return Response({"notes": ["Say why the vendor rejected it."]}, status=http_status.HTTP_400_BAD_REQUEST)
            claim.decided_at = now
        elif new == S.CLOSED:
            resolution = request.data.get("resolution")
            if resolution not in WarrantyClaim.Resolution.values:
                return Response({"resolution": ["Say how the vendor settled it."]}, status=http_status.HTTP_400_BAD_REQUEST)
            claim.resolution = resolution
            raw = request.data.get("recovered_amount")
            if raw not in (None, ""):
                try:
                    claim.recovered_amount = Decimal(str(raw))
                except InvalidOperation:
                    return Response({"recovered_amount": ["Give an amount."]}, status=http_status.HTTP_400_BAD_REQUEST)
            claim.closed_at = now
        elif new == S.WITHDRAWN and not notes:
            return Response({"notes": ["Say why the claim is withdrawn."]}, status=http_status.HTTP_400_BAD_REQUEST)
        claim.status = new
        claim.save()
        line = claim.get_status_display()
        if new == S.SUBMITTED and claim.vendor_reference:
            line += f" (vendor ref {claim.vendor_reference})"
        if new == S.CLOSED:
            line += f": {claim.get_resolution_display()}"
            if claim.recovered_amount is not None:
                line += f", {claim.recovered_amount} recovered"
        if notes:
            line += f" - {notes}"
        self._journal(claim, line)
        claim.refresh_from_db()
        if claim.status in (S.CLOSED, S.REJECTED, S.WITHDRAWN):
            notices.resolve(f"claim:{claim.pk}")
        if claim.raised_by_id:
            notices.tell(
                [claim.raised_by], exclude=[request.user], kind="request_answered",
                title=f"{claim.claim_number}: {claim.get_status_display()}",
                message=line, link="/warranties?tab=claims", data={"claim": str(claim.pk)},
            )
        return Response(WarrantyClaimSerializer(claim, context={"request": request}).data)
