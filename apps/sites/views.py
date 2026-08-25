from django.db.models import Count
from rest_framework import viewsets
from rest_framework.permissions import BasePermission, IsAuthenticated

from common.permissions import AdminManagerWriteElseRead


class IsSuperAdminOrAssignedInstaller(BasePermission):
    """Step/delay actions: the assigned installer (mobile) or a super admin (desktop)."""

    message = "Only the assigned installer or a super admin can do this."

    def has_object_permission(self, request, view, obj):
        installation = obj.installation if hasattr(obj, "installation") else obj
        return (
            getattr(request.user, "role", None) == "super_admin"
            or installation.installed_by_id == request.user.id
        )

from .models import (
    DeviceInstallation,
    InstallationDelay,
    InstallationPhoto,
    InstallationStep,
    Site,
    SiteContact,
    SiteZone,
)
from .serializers import (
    DeviceInstallationDetailSerializer,
    DeviceInstallationListSerializer,
    InstallationDelaySerializer,
    InstallationPhotoSerializer,
    InstallationStepSerializer,
    SiteContactSerializer,
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
        return (
            Site.objects.select_related("client")
            .prefetch_related("contacts")
            .annotate(device_count=Count("devices"))
            .all()
        )

    def get_serializer_class(self):
        if self.action == "list":
            return SiteListSerializer
        return SiteDetailSerializer


class SiteContactViewSet(viewsets.ModelViewSet):
    queryset = SiteContact.objects.select_related("site").all()
    serializer_class = SiteContactSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["site", "is_primary"]
    search_fields = ["name", "email", "phone"]


class SiteZoneViewSet(viewsets.ModelViewSet):
    queryset = SiteZone.objects.select_related("site").all()
    serializer_class = SiteZoneSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["site"]
    search_fields = ["name"]


class DeviceInstallationViewSet(viewsets.ModelViewSet):
    queryset = (
        DeviceInstallation.objects
        .select_related(
            "device", "device__device_model", "device__device_model__brand",
            "device__asset_type", "device__assigned_client", "device__project",
            "installed_by", "site", "zone",
        )
        .prefetch_related("photos", "steps", "delays", "device__clients")
        .all()
    )
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["device", "site", "installed_by", "device__assigned_client", "device__project"]
    search_fields = [
        "device__asset_code", "device__display_name", "device__serial_number",
        "device__assigned_client__name", "device__clients__name",
        "installed_by__first_name", "installed_by__last_name", "installed_by__username",
        "site__name", "position_label",
    ]
    ordering_fields = ["installed_at", "due_date", "completed_at", "created_at"]

    def get_serializer_class(self):
        if self.action == "list":
            return DeviceInstallationListSerializer
        return DeviceInstallationDetailSerializer


class InstallationStepViewSet(viewsets.ModelViewSet):
    queryset = InstallationStep.objects.select_related("installation").all()
    serializer_class = InstallationStepSerializer
    filterset_fields = ["installation", "step_type", "status"]
    ordering_fields = ["step_number"]

    def get_permissions(self):
        # Only the assigned installer (mobile) or a super admin (desktop) may
        # advance a step; only managers add/remove steps.
        if self.action in ("update", "partial_update"):
            return [IsAuthenticated(), IsSuperAdminOrAssignedInstaller()]
        return [IsAuthenticated(), AdminManagerWriteElseRead()]


class InstallationDelayViewSet(viewsets.ModelViewSet):
    queryset = InstallationDelay.objects.select_related(
        "installation", "step", "reported_by"
    ).all()
    serializer_class = InstallationDelaySerializer
    filterset_fields = ["installation", "step", "cause"]
    ordering_fields = ["created_at"]

    def get_permissions(self):
        # Delays are logged by the assigned installer or a super admin;
        # managers can edit/resolve/remove them.
        if self.action == "create":
            return [IsAuthenticated()]
        return [IsAuthenticated(), AdminManagerWriteElseRead()]

    def perform_create(self, serializer):
        installation = serializer.validated_data["installation"]
        user = self.request.user
        if getattr(user, "role", None) != "super_admin" and installation.installed_by_id != user.id:
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied("Only the assigned installer or a super admin can flag a delay.")
        serializer.save(reported_by=user)


class InstallationPhotoViewSet(viewsets.ModelViewSet):
    queryset = InstallationPhoto.objects.select_related("installation").all()
    serializer_class = InstallationPhotoSerializer
    filterset_fields = ["installation", "photo_type"]

    def get_permissions(self):
        # Field techs may attach installation photos; managers can also edit/remove.
        if self.action == "create":
            return [IsAuthenticated()]
        return [IsAuthenticated(), AdminManagerWriteElseRead()]

    def perform_create(self, serializer):
        serializer.save(taken_by=self.request.user)
