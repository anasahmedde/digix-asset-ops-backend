from django.db.models import Count
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from common.permissions import AdminManagerWriteElseRead

from .models import DeviceInstallation, InstallationPhoto, InstallationStep, Site, SiteZone
from .serializers import (
    DeviceInstallationDetailSerializer,
    DeviceInstallationListSerializer,
    InstallationPhotoSerializer,
    InstallationStepSerializer,
    SiteDetailSerializer,
    SiteListSerializer,
    SiteZoneSerializer,
)


class SiteViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["client", "city", "state_province", "country", "is_active"]
    search_fields = ["name", "address", "city", "state_province"]
    ordering_fields = ["name", "created_at"]

    def get_queryset(self):
        return Site.objects.annotate(device_count=Count("devices")).all()

    def get_serializer_class(self):
        if self.action == "list":
            return SiteListSerializer
        return SiteDetailSerializer


class SiteZoneViewSet(viewsets.ModelViewSet):
    queryset = SiteZone.objects.select_related("site").all()
    serializer_class = SiteZoneSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["site"]
    search_fields = ["name"]


class DeviceInstallationViewSet(viewsets.ModelViewSet):
    queryset = (
        DeviceInstallation.objects
        .select_related("device", "device__device_model", "device__device_model__brand", "site", "zone")
        .prefetch_related("photos", "steps")
        .all()
    )
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["device", "site", "installed_by"]
    ordering_fields = ["installed_at"]

    def get_serializer_class(self):
        if self.action == "list":
            return DeviceInstallationListSerializer
        return DeviceInstallationDetailSerializer


class InstallationStepViewSet(viewsets.ModelViewSet):
    queryset = InstallationStep.objects.select_related("installation").all()
    serializer_class = InstallationStepSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["installation", "step_type", "status"]
    ordering_fields = ["step_number"]


class InstallationPhotoViewSet(viewsets.ModelViewSet):
    queryset = InstallationPhoto.objects.select_related("installation").all()
    serializer_class = InstallationPhotoSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["installation", "photo_type"]
