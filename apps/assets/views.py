import uuid

from django.db.models import Count, F, Q
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.exports import EXPORT_MAX_ROWS, export_params, log_export, xlsx_response
from common.permissions import AdminManagerWriteElseRead

from .labels import render_label, render_labels_pdf

from .models import (
    AssetCode,
    AssetComponent,
    AssetType,
    Brand,
    Device,
    DeviceImage,
    DeviceLifecycleEvent,
    DeviceModel,
    MaterialType,
)
from .serializers import (
    AssetCodeSerializer,
    AssetComponentSerializer,
    AssetTypeSerializer,
    BrandSerializer,
    DeviceDetailSerializer,
    DeviceImageSerializer,
    DeviceLifecycleEventSerializer,
    DeviceListSerializer,
    DeviceModelSerializer,
    DeviceTransitionSerializer,
    MaterialTypeSerializer,
)

# Who may move assets through the status machine: oversight roles plus the
# warehouse team (who receive stock, dispatch and process RMAs).
DEVICE_TRANSITION_ROLES = ("super_admin", "group_head", "ops_manager", "supervisor", "warehouse")

# Hard cap on a bulk label print — one PDF page per device.
LABEL_BATCH_MAX = 200


def persist_asset_code(device, fmt, label_size=None):
    """Create or refresh the device's current AssetCode row for ``fmt``.

    Reuses the ``is_current`` row per (device, format) so repeated prints
    don't stack duplicate records — shared by the single-label and bulk
    label actions so both leave the same ledger behind.
    """
    code_obj = AssetCode.objects.filter(
        device=device, format=fmt, is_current=True
    ).first()
    if code_obj is None:
        code_obj = AssetCode(device=device, format=fmt)
    code_obj.label_size = label_size or code_obj.label_size or "60x30"

    content = render_label(device, fmt)
    code_obj.generated_file.save(content.name, content, save=True)
    return code_obj


class AssetTypeViewSet(viewsets.ModelViewSet):
    queryset = AssetType.objects.all()
    serializer_class = AssetTypeSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["is_active"]
    search_fields = ["name", "code"]
    ordering_fields = ["name", "created_at"]


class BrandViewSet(viewsets.ModelViewSet):
    queryset = Brand.objects.all()
    serializer_class = BrandSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["is_active"]
    search_fields = ["name"]


class DeviceModelViewSet(viewsets.ModelViewSet):
    queryset = DeviceModel.objects.select_related("brand").all()
    serializer_class = DeviceModelSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["brand", "is_active"]
    search_fields = ["name", "model_number"]


class MaterialTypeViewSet(viewsets.ModelViewSet):
    queryset = MaterialType.objects.all()
    serializer_class = MaterialTypeSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["category"]
    search_fields = ["name", "category"]


class DeviceViewSet(viewsets.ModelViewSet):
    queryset = Device.objects.select_related(
        "asset_type", "device_model", "device_model__brand", "current_site",
        "assigned_client", "supplier", "assigned_technician", "installed_by", "project",
    ).prefetch_related("images", "warranties", "clients").all()
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = [
        "status", "source", "asset_type", "device_model", "current_site",
        "assigned_client", "assigned_technician",
    ]
    search_fields = [
        "asset_code", "serial_number", "mobile_id", "mac_address", "display_name",
        "device_model__name", "current_site__name", "assigned_client__name",
    ]
    ordering_fields = ["created_at", "asset_code", "status", "installation_date"]

    def get_queryset(self):
        qs = super().get_queryset()
        # Vendor scope (XC-04): portal users only see devices that appear in
        # tickets assigned to their supplier or installations their supplier
        # is doing — read-only (vendors are in no write-role group).
        user = self.request.user
        if getattr(user, "role", "") == "vendor" and not user.is_superuser:
            if not user.supplier_id:
                return qs.none()
            qs = qs.filter(
                Q(tickets__assigned_vendor_id=user.supplier_id)
                | Q(linked_tickets__assigned_vendor_id=user.supplier_id)
                | Q(installations__vendor_id=user.supplier_id)
            ).distinct()
        # ?flag=operational|warranty_expired — dashboard drill-downs; lives
        # here (not filterset_fields) so list AND export share it. Unknown
        # values are ignored.
        flag = self.request.query_params.get("flag")
        if flag == "operational":
            qs = qs.filter(status__in=[Device.Status.ACTIVE, Device.Status.INSTALLED])
        elif flag == "warranty_expired":
            qs = (
                qs.filter(warranties__isnull=False)
                .exclude(warranties__status="active")
                .distinct()
            )
        return qs

    def get_serializer_class(self):
        if self.action == "list":
            return DeviceListSerializer
        return DeviceDetailSerializer

    def get_permissions(self):
        # The transition action carries its own role gate (supervisor and
        # warehouse may transition but are read-only elsewhere).
        if self.action == "transition":
            return [IsAuthenticated()]
        return super().get_permissions()

    # ── Status transition (guarded state machine) ─────────────────────

    @action(detail=True, methods=["post"], url_path="transition")
    def transition(self, request, pk=None):
        """Move the device through the status machine, journalling the flip.

        The reason is mandatory — signals write it into the lifecycle event
        and the audit trail alongside who performed the change.
        """
        device = self.get_object()
        if getattr(request.user, "role", "") not in DEVICE_TRANSITION_ROLES:
            return Response(
                {"detail": "You do not have permission to change asset status."},
                status=status.HTTP_403_FORBIDDEN,
            )

        ser = DeviceTransitionSerializer(data=request.data, context={"device": device})
        ser.is_valid(raise_exception=True)

        device.status = ser.validated_data["status"]
        device._transition_user = request.user
        device._transition_reason = ser.validated_data["reason"]
        device.save(update_fields=["status", "updated_at"])

        device.refresh_from_db()
        return Response(DeviceDetailSerializer(device, context={"request": request}).data)

    @action(detail=True, methods=["get"])
    def lifecycle(self, request, pk=None):
        device = self.get_object()
        events = device.lifecycle_events.order_by("-created_at")
        page = self.paginate_queryset(events)
        if page is not None:
            serializer = DeviceLifecycleEventSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = DeviceLifecycleEventSerializer(events, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["get"])
    def status_summary(self, request):
        """Device count per status for dashboard widgets."""
        summary = Device.objects.values("status").annotate(count=Count("id")).order_by("status")
        return Response(summary)

    @action(detail=False, methods=["get"])
    def dashboard_stats(self, request):
        """Comprehensive device stats for the main dashboard."""
        total = Device.objects.count()
        by_status = dict(
            Device.objects.values_list("status").annotate(c=Count("id")).values_list("status", "c")
        )
        active_count = by_status.get("active", 0) + by_status.get("installed", 0)

        by_city = list(
            Device.objects.filter(current_site__isnull=False)
            .values(city=F("current_site__city"))
            .annotate(count=Count("id"))
            .order_by("-count")[:10]
        )
        by_region = list(
            Device.objects.filter(current_site__isnull=False)
            .values(region=F("current_site__state_province"))
            .annotate(count=Count("id"))
            .order_by("-count")
        )
        by_model_type = list(
            Device.objects.values(screen_type=F("device_model__screen_type"))
            .annotate(count=Count("id"))
            .order_by("-count")
        )

        return Response({
            "total": total,
            "working": active_count,
            "installed": by_status.get("installed", 0) + by_status.get("active", 0),
            "out_of_order": by_status.get("decommissioned", 0) + by_status.get("lost_stolen", 0),
            "under_maintenance": by_status.get("under_maintenance", 0),
            "in_stock": by_status.get("in_stock", 0),
            "by_status": by_status,
            "by_city": by_city,
            "by_region": by_region,
            "by_model_type": by_model_type,
        })

    @action(detail=False, methods=["get"])
    def map_data(self, request):
        """Device locations with status for map rendering."""
        devices = (
            Device.objects.filter(
                current_site__isnull=False,
                current_site__latitude__isnull=False,
                current_site__longitude__isnull=False,
            )
            .select_related("current_site")
            .annotate(
                open_tickets=Count(
                    "tickets",
                    filter=~Q(tickets__status__in=["closed", "approved", "rejected"]),
                )
            )
            .values(
                "id", "asset_code", "status", "open_tickets",
                "current_site__id",
                "current_site__name", "current_site__city",
                "current_site__state_province", "current_site__country",
                "current_site__latitude", "current_site__longitude",
            )
        )
        return Response(list(devices))

    @action(detail=False, methods=["get"], url_path="export")
    def export(self, request):
        """Excel export of the asset registry (XC-01).

        Respects the caller's filters/search/ordering; capped at
        EXPORT_MAX_ROWS; journalled in the audit trail.
        """
        from .serializers import _warranty_status

        qs = self.filter_queryset(self.get_queryset())[:EXPORT_MAX_ROWS]
        columns = [
            "Asset Code", "Serial Number", "Name", "Model", "Asset Type",
            "Status", "Source", "Batch Number", "Client", "Site", "Project",
            "Purchase Date", "Purchase Price", "Warranty Status",
        ]
        rows = []
        for d in qs:
            rows.append([
                d.asset_code,
                d.serial_number,
                d.display_name,
                str(d.device_model) if d.device_model_id else "",
                d.asset_type.name if d.asset_type_id else "",
                d.get_status_display(),
                d.get_source_display(),
                d.batch_number,
                d.assigned_client.name if d.assigned_client_id else "",
                d.current_site.name if d.current_site_id else "",
                d.project.name if d.project_id else "",
                d.purchase_date,
                d.purchase_price,
                _warranty_status(d),
            ])
        log_export(request.user, "device", len(rows), export_params(request))
        return xlsx_response("assets", "Assets", columns, rows)

    @action(detail=True, methods=["post"], url_path="label")
    def label(self, request, pk=None):
        """Generate (or refresh) the printable QR/barcode label for this device.

        The label encodes ``asset_code``, which the mobile scanner resolves via
        the device search endpoint. Re-generating reuses the current AssetCode
        row so repeated prints don't stack duplicate records.
        """
        device = self.get_object()
        fmt = request.data.get("format", AssetCode.LabelFormat.QR)
        if fmt not in AssetCode.LabelFormat.values:
            return Response(
                {"format": [f"Must be one of: {', '.join(AssetCode.LabelFormat.values)}."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        code_obj = persist_asset_code(device, fmt, request.data.get("label_size"))
        return Response(AssetCodeSerializer(code_obj, context={"request": request}).data)

    @action(detail=False, methods=["post"], url_path="labels")
    def labels(self, request):
        """Bulk label print (WF-05): one PDF, one label per page.

        Body: ``{"ids": [uuid, ...], "format": "qr"|"code128"}``. Uses the
        same rendering, AssetCode ledger and role gate as the single-label
        action (POST under AdminManagerWriteElseRead → managers only).
        Capped at ``LABEL_BATCH_MAX`` devices per request; any id that
        doesn't resolve fails the whole batch with a 400 naming it.
        """
        ids = request.data.get("ids")
        if not isinstance(ids, list) or not ids:
            return Response(
                {"ids": ["Provide a non-empty list of device ids."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(ids) > LABEL_BATCH_MAX:
            return Response(
                {"ids": [f"At most {LABEL_BATCH_MAX} devices per batch (got {len(ids)})."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            ids = [uuid.UUID(str(i)) for i in ids]
        except (ValueError, AttributeError, TypeError):
            return Response(
                {"ids": ["All ids must be valid UUIDs."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Repeated ids collapse to one page/label — dedupe before resolving
        # so the missing-id check compares against distinct ids only.
        ids = list(dict.fromkeys(ids))

        fmt = request.data.get("format", AssetCode.LabelFormat.QR)
        if fmt not in AssetCode.LabelFormat.values:
            return Response(
                {"format": [f"Must be one of: {', '.join(AssetCode.LabelFormat.values)}."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        devices = list(
            self.get_queryset().filter(pk__in=ids).order_by("asset_code")
        )
        # A partial batch must not silently print fewer pages — name every
        # id that didn't resolve so the caller can fix the selection.
        found = {d.pk for d in devices}
        missing = [str(i) for i in ids if i not in found]
        if missing:
            return Response(
                {"ids": [f"Unknown device ids: {', '.join(missing)}."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Persist the ledger exactly like the single-label action so bulk
        # prints leave the same AssetCode trail behind.
        label_size = request.data.get("label_size")
        for device in devices:
            persist_asset_code(device, fmt, label_size)

        pdf = render_labels_pdf(devices, fmt)
        filename = f"labels-{fmt}-{timezone.localdate().isoformat()}.pdf"
        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class AssetComponentViewSet(viewsets.ModelViewSet):
    queryset = AssetComponent.objects.select_related("device", "supplier").prefetch_related("warranties").all()
    serializer_class = AssetComponentSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["device", "component_type"]
    search_fields = ["name", "serial_number"]


class DeviceImageViewSet(viewsets.ModelViewSet):
    queryset = DeviceImage.objects.select_related("device").all()
    serializer_class = DeviceImageSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["device", "is_primary"]


class DeviceLifecycleEventViewSet(viewsets.ModelViewSet):
    queryset = DeviceLifecycleEvent.objects.select_related("device", "performed_by").all()
    serializer_class = DeviceLifecycleEventSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["device", "event_type"]
    ordering_fields = ["created_at"]


class AssetCodeViewSet(viewsets.ModelViewSet):
    queryset = AssetCode.objects.select_related("device").all()
    serializer_class = AssetCodeSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["device", "format", "is_current"]
