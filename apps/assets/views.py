from django.db.models import Count, F
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.permissions import AdminManagerWriteElseRead

from .models import (
    AssetCode,
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
            .values(
                "id", "asset_code", "status",
                "current_site__id",
                "current_site__name", "current_site__city",
                "current_site__state_province", "current_site__country",
                "current_site__latitude", "current_site__longitude",
            )
        )
        return Response(list(devices))


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
