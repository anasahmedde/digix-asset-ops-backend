from django.db.models import Count, F, Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.permissions import AdminManagerWriteElseRead

from .labels import render_label

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
    MaterialTypeSerializer,
)


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
        "assigned_client", "supplier", "assigned_technician", "installed_by",
    ).prefetch_related("images", "warranties").all()
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = [
        "status", "asset_type", "device_model", "current_site",
        "assigned_client", "assigned_technician",
    ]
    search_fields = ["asset_code", "serial_number", "mobile_id", "mac_address", "display_name"]
    ordering_fields = ["created_at", "asset_code", "status", "installation_date"]

    def get_serializer_class(self):
        if self.action == "list":
            return DeviceListSerializer
        return DeviceDetailSerializer

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

        code_obj = AssetCode.objects.filter(
            device=device, format=fmt, is_current=True
        ).first()
        if code_obj is None:
            code_obj = AssetCode(device=device, format=fmt)
        code_obj.label_size = request.data.get("label_size", code_obj.label_size or "60x30")

        content = render_label(device, fmt)
        code_obj.generated_file.save(content.name, content, save=True)

        return Response(AssetCodeSerializer(code_obj, context={"request": request}).data)


class AssetComponentViewSet(viewsets.ModelViewSet):
    queryset = AssetComponent.objects.select_related("device").all()
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
