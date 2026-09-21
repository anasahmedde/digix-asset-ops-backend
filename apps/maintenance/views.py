from rest_framework import serializers as drf_serializers
from rest_framework import status as drf_status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import SAFE_METHODS, BasePermission, IsAuthenticated
from rest_framework.response import Response

from common.permissions import TechnicianCanCreate

from .models import (
    MaintenancePartRequest,
    MaintenanceRecord,
    MaintenanceRecordPhoto,
    MaintenanceSchedule,
)
from .serializers import (
    MaintenancePartDecisionSerializer,
    MaintenancePartRequestSerializer,
    MaintenanceRecordPhotoSerializer,
    MaintenanceRecordSerializer,
    MaintenanceScheduleSerializer,
)

# Who may answer a technician's request for parts. Raising one is the
# technician's job; releasing stock against it is not.
DECIDING_ROLES = ("super_admin", "group_head", "ops_manager", "supervisor")
# Who may raise or withdraw one: the people who actually attend the job, and
# the managers above them.
ASKING_ROLES = DECIDING_ROLES + ("technician",)


class CanAskOrAnswerForParts(BasePermission):
    """Technicians ask, supervisors and managers answer; everyone else reads.

    The shared technician permission is shaped for tickets and does not know
    about deciding or withdrawing, so this viewset states its own rule rather
    than widening one that other screens depend on.
    """

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if request.method in SAFE_METHODS:
            return True
        role = getattr(user, "role", None)
        if view.action == "decide":
            return role in DECIDING_ROLES
        return role in ASKING_ROLES


class MaintenancePartRequestViewSet(viewsets.ModelViewSet):
    """Parts a technician has asked for on a job, and the answers given."""

    queryset = MaintenancePartRequest.objects.select_related(
        "schedule", "schedule__device", "item", "item__material_type",
        "unit_type", "requested_by", "decided_by", "issuance_request",
    ).all()
    serializer_class = MaintenancePartRequestSerializer
    permission_classes = [IsAuthenticated, CanAskOrAnswerForParts]
    filterset_fields = ["schedule", "status", "requested_by"]
    search_fields = ["name", "schedule__title"]
    ordering_fields = ["created_at"]

    def perform_create(self, serializer):
        serializer.save(requested_by=self.request.user)

    def perform_destroy(self, instance):
        """A line can be withdrawn while nobody has answered it yet."""
        if instance.status != MaintenancePartRequest.Status.REQUESTED:
            raise drf_serializers.ValidationError(
                {"detail": "A line that has been answered stays on the record."}
            )
        instance.delete()

    @action(detail=True, methods=["post"])
    def decide(self, request, pk=None):
        """Approve this line, for all or part of what was asked, or reject it.

        Approving raises the request the store will issue against; it does not
        move any stock itself.
        """
        from .parts import decide as decide_line

        if getattr(request.user, "role", None) not in DECIDING_ROLES:
            return Response(
                {"detail": "Only a supervisor or above can answer a request for parts."},
                status=drf_status.HTTP_403_FORBIDDEN,
            )
        ser = MaintenancePartDecisionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        line = decide_line(
            self.get_object(),
            user=request.user,
            approve=ser.validated_data["approve"],
            quantity=ser.validated_data.get("quantity"),
            note=ser.validated_data.get("note", ""),
        )
        return Response(self.get_serializer(line).data)


class MaintenanceScheduleViewSet(viewsets.ModelViewSet):
    queryset = MaintenanceSchedule.objects.select_related(
        "device", "site", "assigned_to"
    ).prefetch_related("vendors").all()
    serializer_class = MaintenanceScheduleSerializer
    permission_classes = [IsAuthenticated, TechnicianCanCreate]
    filterset_fields = [
        "maintenance_type", "frequency", "status", "is_active", "assigned_to", "device", "priority",
    ]
    search_fields = ["title", "device__asset_code", "device__display_name"]
    ordering_fields = ["next_due", "created_at", "priority"]

    @action(detail=False, methods=["get"])
    def map_data(self, request):
        """Sites with active maintenance schedules for map markers."""
        sites = (
            MaintenanceSchedule.objects.filter(
                is_active=True,
                site__isnull=False,
                site__latitude__isnull=False,
                site__longitude__isnull=False,
            )
            .select_related("site")
            .values(
                "id", "title", "maintenance_type", "frequency", "next_due",
                "device", "site__id", "site__name", "site__city",
                "site__state_province", "site__country",
                "site__latitude", "site__longitude",
            )
            .distinct()
        )
        return Response(list(sites))


    def perform_destroy(self, instance):
        """A fault is closed, not deleted.

        Deleting the open job for an asset that is still out of service leaves
        it stranded: nothing tracks the repair, and nothing is left to complete
        to bring it back into service. Complete it instead.
        """
        from rest_framework.exceptions import ValidationError

        from apps.assets.models import Device

        if (
            instance.maintenance_type == MaintenanceSchedule.MaintenanceType.CORRECTIVE
            and instance.status != MaintenanceSchedule.Status.COMPLETED
            and instance.device_id
            and instance.device.status == Device.Status.UNDER_MAINTENANCE
        ):
            raise ValidationError(
                "This asset is out of service on this job — complete it instead, "
                "which puts the asset back into service."
            )
        instance.delete()


class MaintenanceRecordViewSet(viewsets.ModelViewSet):
    queryset = MaintenanceRecord.objects.select_related(
        "schedule", "performed_by"
    ).prefetch_related("components_used", "photos").all()
    serializer_class = MaintenanceRecordSerializer
    permission_classes = [IsAuthenticated, TechnicianCanCreate]
    filterset_fields = ["schedule", "status", "performed_by"]
    ordering_fields = ["performed_at"]

    def perform_create(self, serializer):
        record = serializer.save(performed_by=self.request.user)
        # A completed visit rolls its schedule to the next cycle.
        if record.status == MaintenanceRecord.Status.COMPLETED:
            record.schedule.advance_after_completion(record.performed_at.date())
            # Closing a corrective job is what returns the asset to Active.
            from .services import return_to_service_if_done

            return_to_service_if_done(record, self.request.user)


class MaintenanceRecordPhotoViewSet(viewsets.ModelViewSet):
    queryset = MaintenanceRecordPhoto.objects.select_related("record").all()
    serializer_class = MaintenanceRecordPhotoSerializer
    permission_classes = [IsAuthenticated, TechnicianCanCreate]
    filterset_fields = ["record"]

    def perform_create(self, serializer):
        serializer.save(taken_by=self.request.user)
