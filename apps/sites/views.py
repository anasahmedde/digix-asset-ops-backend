from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from rest_framework import status as drf_status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response

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
    HandoverRecord,
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
    HandoverCreateSerializer,
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
            "installed_by", "vendor", "site", "zone",
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

    def get_queryset(self):
        qs = super().get_queryset()
        # ?escalated=true|false — installations with a non-empty escalation ledger.
        escalated = self.request.query_params.get("escalated")
        if escalated is not None:
            value = escalated.strip().lower()
            if value in ("true", "1"):
                qs = qs.exclude(escalation_state={})
            elif value in ("false", "0"):
                qs = qs.filter(escalation_state={})
        return qs

    def get_serializer_class(self):
        if self.action == "list":
            return DeviceInstallationListSerializer
        return DeviceInstallationDetailSerializer

    def get_permissions(self):
        # The handover action carries its own gate (assigned installer or
        # HANDOVER_ROLES) — the viewset's manager-write permission would
        # otherwise reject the installer/supervisor before it ever runs.
        if self.action == "handover":
            return [IsAuthenticated()]
        return super().get_permissions()

    HANDOVER_ROLES = ("super_admin", "group_head", "ops_manager", "supervisor")

    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser, FormParser, JSONParser])
    def handover(self, request, pk=None):
        """Formal handover (WF-12): record acceptance, assign client + site to
        the asset, complete the handover step and move the asset to Active."""
        installation = self.get_object()
        user = request.user
        if (
            getattr(user, "role", None) not in self.HANDOVER_ROLES
            and installation.installed_by_id != user.id
        ):
            return Response(
                {"detail": "Only the assigned installer or operations management can hand over."},
                status=drf_status.HTTP_403_FORBIDDEN,
            )
        if getattr(installation, "handover", None):
            return Response(
                {"detail": "This installation has already been handed over."},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )
        pending = [
            step.custom_label or step.get_step_type_display()
            for step in installation.steps.exclude(step_type=InstallationStep.StepType.HANDOVER)
            if step.status not in (InstallationStep.StepStatus.COMPLETED, InstallationStep.StepStatus.SKIPPED)
        ]
        if pending:
            return Response(
                {"detail": f"Complete the remaining steps before handover: {', '.join(pending)}."},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )

        ser = HandoverCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        device = installation.device
        client = ser.validated_data.get("client") or device.assigned_client
        if client is None:
            return Response(
                {"client": "The asset has no client yet — pick the client receiving it."},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            # Row-lock and re-check under the lock: two concurrent submits
            # must yield one record and one clean 400, not an IntegrityError.
            installation = (
                DeviceInstallation.objects.select_for_update()
                .select_related("device", "site")
                .get(pk=installation.pk)
            )
            if getattr(installation, "handover", None):
                return Response(
                    {"detail": "This installation has already been handed over."},
                    status=drf_status.HTTP_400_BAD_REQUEST,
                )
            device = installation.device
            record = HandoverRecord.objects.create(
                installation=installation,
                device=device,
                client=client,
                site=installation.site,
                handover_date=ser.validated_data.get("handover_date") or timezone.localdate(),
                accepted_by_name=ser.validated_data["accepted_by_name"],
                acceptance_notes=ser.validated_data.get("acceptance_notes", ""),
                signature=ser.validated_data.get("signature"),
                performed_by=user,
            )
            device.assigned_client = client
            device.current_site = installation.site
            device.installation_date = record.handover_date
            device.save(update_fields=["assigned_client", "current_site", "installation_date", "updated_at"])

            for image in request.FILES.getlist("photos"):
                InstallationPhoto.objects.create(
                    installation=installation,
                    photo_type=InstallationPhoto.PhotoType.HANDOVER,
                    image=image,
                    taken_by=user,
                )

            # Completing the handover step stamps completed_at, re-anchors the
            # client warranty to the record's date and journals the flip.
            step = installation.steps.filter(step_type=InstallationStep.StepType.HANDOVER).first()
            if step and step.status not in (
                InstallationStep.StepStatus.COMPLETED, InstallationStep.StepStatus.SKIPPED
            ):
                step.status = InstallationStep.StepStatus.COMPLETED
                step.save()

            # The step-save signal only anchors while completed_at is unset —
            # when the checklist was already closed out (mobile flow) the
            # formal record's date must still win, so re-anchor explicitly.
            from .signals import _anchor_client_warranties

            installation.refresh_from_db()
            _anchor_client_warranties(installation)

            device.refresh_from_db()
            if device.status != "active":
                device._transition_user = user
                device._transition_reason = f"Handover accepted by {record.accepted_by_name}"
                device.status = "active"
                device.save(update_fields=["status", "updated_at"])

        installation.refresh_from_db()
        installation._prefetched_objects_cache = {}
        return Response(
            DeviceInstallationDetailSerializer(installation, context=self.get_serializer_context()).data,
            status=drf_status.HTTP_201_CREATED,
        )


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
