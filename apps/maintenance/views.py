from rest_framework import serializers as drf_serializers
from rest_framework import status as drf_status
from common.noops import RefusesSilentNoOps
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import SAFE_METHODS, BasePermission, IsAuthenticated
from rest_framework.response import Response

from common.permissions import MANAGER_ROLES, CapabilityGate, TechnicianCanCreate

from .models import (
    MaintenancePartRequest,
    MaintenanceRecord,
    MaintenanceRecordPhoto,
    MaintenanceSchedule,
    MaintenanceVisit,
)
from .serializers import (
    MaintenancePartDecisionSerializer,
    MaintenancePartRequestSerializer,
    MaintenanceRecordPhotoSerializer,
    MaintenanceRecordSerializer,
    MaintenanceScheduleSerializer,
    MaintenanceVisitSerializer,
)

# Who may answer a technician's request for parts. Raising one is the
# technician's job; releasing stock against it is not.
DECIDING_ROLES = ("super_admin", "group_head", "ops_manager", "supervisor")
# Who may raise or withdraw one: the people who actually attend the job, and
# the managers above them.
ASKING_ROLES = DECIDING_ROLES + ("technician",)


def _answers_for(user, line) -> bool:
    """May this person answer this particular line?

    Being a supervisor is not the same as being *their* supervisor. The
    organogram already records who answers to whom, so a line is answered by
    the asker's own reporting line and nobody else's; Operations answers for
    everyone, which is what makes them Operations.
    """
    if getattr(user, "is_superuser", False) or getattr(user, "role", None) in MANAGER_ROLES:
        return True
    asker = line.requested_by
    return asker is not None and user.manages(asker)


def _may_withdraw(user, line) -> bool:
    """The person who asked, or somebody above them."""
    if getattr(user, "is_superuser", False) or getattr(user, "role", None) in MANAGER_ROLES:
        return True
    if line.requested_by_id == user.pk:
        return True
    return line.requested_by is not None and user.manages(line.requested_by)


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
        # A line is asked for on a round, and the round being planned is the
        # one it is for.
        schedule = serializer.validated_data.get("schedule")
        serializer.save(
            requested_by=self.request.user,
            visit=schedule.open_visit() if schedule is not None else None,
        )

    def perform_destroy(self, instance):
        """Withdrawing a line takes it off the queue. It does not erase it.

        Two things were wrong with deleting the row. Any technician could
        withdraw any other technician's line, and the line then left no trace
        — but what was asked for and what became of it is the whole point of
        a job history, so a withdrawn line stays on the record saying so.
        """
        from django.utils import timezone

        user = self.request.user
        if instance.status != MaintenancePartRequest.Status.REQUESTED:
            raise drf_serializers.ValidationError(
                {"detail": "A line that has been answered stays on the record."}
            )
        if not _may_withdraw(user, instance):
            raise PermissionDenied("You can only withdraw a line you asked for.")
        instance.status = MaintenancePartRequest.Status.CANCELLED
        instance.decided_by = user
        instance.decided_at = timezone.now()
        instance.decision_note = f"Withdrawn by {user.get_full_name() or user.username}"
        instance.save(update_fields=[
            "status", "decided_by", "decided_at", "decision_note", "updated_at",
        ])

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
        asked = self.get_object()
        # Nobody signs off their own request, whatever they hold. A supervisor
        # who needs a part asks the person they report to, the same as anyone.
        if asked.requested_by_id == request.user.pk:
            return Response(
                {"detail": "You cannot approve your own request. Ask the person you report to."},
                status=drf_status.HTTP_403_FORBIDDEN,
            )
        if not _answers_for(request.user, asked):
            return Response(
                {"detail": "This line is not from your team — their own supervisor answers it."},
                status=drf_status.HTTP_403_FORBIDDEN,
            )
        ser = MaintenancePartDecisionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        line = decide_line(
            asked,
            user=request.user,
            approve=ser.validated_data["approve"],
            quantity=ser.validated_data.get("quantity"),
            note=ser.validated_data.get("note", ""),
        )
        return Response(self.get_serializer(line).data)


class MaintenanceScheduleViewSet(RefusesSilentNoOps, viewsets.ModelViewSet):
    # Reading this is a permission, not just a menu entry.
    read_capability = "view_tickets"
    queryset = MaintenanceSchedule.objects.select_related(
        "device", "site", "assigned_to", "ticket"
    ).prefetch_related("vendors", "visits__assigned_to").all()
    serializer_class = MaintenanceScheduleSerializer
    permission_classes = [IsAuthenticated, TechnicianCanCreate, CapabilityGate]
    filterset_fields = [
        "maintenance_type", "frequency", "status", "is_active", "assigned_to", "device", "priority",
        "ticket",
    ]
    search_fields = ["title", "device__asset_code", "device__display_name"]
    ordering_fields = ["next_due", "created_at", "priority"]

    def get_object(self):
        """A technician works their own rounds, not everybody else's.

        Creating a round is theirs to do — they are the ones who find work
        that needs scheduling. Editing one was not scoped at all, so any
        technician could rewrite any round in the company, including its
        dates and who is on it.
        """
        obj = super().get_object()
        if self.request.method in SAFE_METHODS:
            return obj
        user = self.request.user
        if getattr(user, "role", None) == "technician" and not user.is_superuser:
            if obj.assigned_to_id not in (user.pk, None):
                raise PermissionDenied("This round is not yours to change.")
        return obj

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


class CanPlanOrStartARound(BasePermission):
    """Planning a round is a supervisor's call; attending one is not.

    Rounds are opened by the schedule itself, so nobody creates or deletes one
    from outside: they are planned, started and closed out.
    """

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method in SAFE_METHODS:
            return True
        if view.action == "start":
            return getattr(request.user, "role", None) in ASKING_ROLES
        if view.action in ("update", "partial_update"):
            return getattr(request.user, "role", None) in DECIDING_ROLES
        return False


class MaintenanceVisitViewSet(viewsets.ModelViewSet):
    """The rounds of a schedule: when each is due and who is going.

    A schedule is an arrangement that comes round again and again, so who
    attends is decided one round at a time rather than once for all of them.
    """

    queryset = MaintenanceVisit.objects.select_related(
        "schedule", "schedule__device", "schedule__site", "assigned_to",
        "record", "record__performed_by",
    ).prefetch_related("record__components_used", "record__photos").all()
    serializer_class = MaintenanceVisitSerializer
    permission_classes = [IsAuthenticated, CanPlanOrStartARound]
    filterset_fields = ["schedule", "status", "assigned_to"]
    ordering_fields = ["due_date", "created_at"]

    def perform_destroy(self, instance):
        raise drf_serializers.ValidationError(
            {"detail": "A round is completed or skipped, not deleted."}
        )

    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        """Somebody is on site: the round is under way, and so is the job."""
        from django.utils import timezone

        visit = self.get_object()
        if visit.status != MaintenanceVisit.Status.PLANNED:
            return Response(
                {"detail": f"This round is already {visit.get_status_display().lower()}."},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )
        visit.status = MaintenanceVisit.Status.IN_PROGRESS
        visit.started_at = timezone.now()
        visit.save(update_fields=["status", "started_at", "updated_at"])
        schedule = visit.schedule
        if schedule.status != MaintenanceSchedule.Status.IN_PROCESS:
            schedule.status = MaintenanceSchedule.Status.IN_PROCESS
            schedule.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(visit).data)


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
            # Closing a corrective job is what returns the asset to Active,
            # and what tells the ticket that raised it that the work is done.
            from .services import report_back_to_ticket, return_to_service_if_done

            return_to_service_if_done(record, self.request.user)
            report_back_to_ticket(record, self.request.user)


class MaintenanceRecordPhotoViewSet(viewsets.ModelViewSet):
    queryset = MaintenanceRecordPhoto.objects.select_related("record").all()
    serializer_class = MaintenanceRecordPhotoSerializer
    permission_classes = [IsAuthenticated, TechnicianCanCreate]
    filterset_fields = ["record"]

    def perform_create(self, serializer):
        serializer.save(taken_by=self.request.user)
