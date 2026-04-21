from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import AssetCode, Brand, Device, DeviceLifecycleEvent, DeviceModel, MaterialType
from .serializers import (
    AssetCodeSerializer,
    BrandSerializer,
    DeviceDetailSerializer,
    DeviceLifecycleEventSerializer,
    DeviceListSerializer,
    DeviceModelSerializer,
    MaterialTypeSerializer,
)


class BrandViewSet(viewsets.ModelViewSet):
    queryset = Brand.objects.all()
    serializer_class = BrandSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["is_active"]
    search_fields = ["name"]


class DeviceModelViewSet(viewsets.ModelViewSet):
    queryset = DeviceModel.objects.select_related("brand").all()
    serializer_class = DeviceModelSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["brand", "is_active"]
    search_fields = ["name", "model_number"]


class MaterialTypeViewSet(viewsets.ModelViewSet):
    queryset = MaterialType.objects.all()
    serializer_class = MaterialTypeSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["category"]
    search_fields = ["name", "category"]


class DeviceViewSet(viewsets.ModelViewSet):
    queryset = Device.objects.select_related(
        "device_model", "device_model__brand", "current_site", "assigned_client"
    ).all()
    permission_classes = [IsAuthenticated]
    filterset_fields = ["status", "device_model", "current_site", "assigned_client", "assigned_technician"]
    search_fields = ["asset_code", "serial_number", "mobile_id", "mac_address"]
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
        from django.db.models import Count

        summary = Device.objects.values("status").annotate(count=Count("id")).order_by("status")
        return Response(summary)


class DeviceLifecycleEventViewSet(viewsets.ModelViewSet):
    queryset = DeviceLifecycleEvent.objects.select_related("device", "performed_by").all()
    serializer_class = DeviceLifecycleEventSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["device", "event_type"]
    ordering_fields = ["created_at"]


class AssetCodeViewSet(viewsets.ModelViewSet):
    queryset = AssetCode.objects.select_related("device").all()
    serializer_class = AssetCodeSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["device", "format", "is_current"]
