from django.db.models import Count
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from .models import DeviceInstallation, InstallationPhoto, Site, SiteZone
from .serializers import (
    DeviceInstallationSerializer,
    InstallationPhotoSerializer,
    SiteDetailSerializer,
    SiteListSerializer,
    SiteZoneSerializer,
)


class SiteViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    filterset_fields = ["client", "city", "country", "is_active"]
    search_fields = ["name", "address", "city"]
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
    permission_classes = [IsAuthenticated]
    filterset_fields = ["site"]
    search_fields = ["name"]


class DeviceInstallationViewSet(viewsets.ModelViewSet):
    queryset = DeviceInstallation.objects.select_related("device", "site", "zone").prefetch_related("photos").all()
    serializer_class = DeviceInstallationSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["device", "site", "installed_by"]
    ordering_fields = ["installed_at"]


class InstallationPhotoViewSet(viewsets.ModelViewSet):
    queryset = InstallationPhoto.objects.select_related("installation").all()
    serializer_class = InstallationPhotoSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["installation", "photo_type"]
