from django.db.models import DateField
from django.db.models.functions import Cast, Coalesce
from rest_framework import serializers as drf_serializers
from rest_framework import status as drf_status
from common.noops import RefusesSilentNoOps
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import SAFE_METHODS, BasePermission, IsAuthenticated
from rest_framework.response import Response

from . import lifecycle


def _where_from(data):
    """The latitude and longitude a client offered, rounded to the column.

    A browser hands back every digit it has and the column keeps seven, so
    this is the same rounding the map pins get — and a reading that is not
    a number at all is simply no reading.
    """
    from common.geo import Coordinate

    field = Coordinate()
    out = []
    for name in ("latitude", "longitude"):
        raw = data.get(name)
        try:
            out.append(field.to_internal_value(raw) if raw not in (None, "", "null") else None)
        except Exception:
            out.append(None)
    # Half a fix is no fix: a latitude with no longitude is not a place.
    return tuple(out) if all(v is not None for v in out) else (None, None)


def _as_date(value, field):
    """JSON carries a date as text; the lifecycle wants a date.

    Parsing it here rather than deeper down means a typed date is the only
    kind the state machine ever sees.
    """
    from rest_framework.fields import DateField

    from common.dates import refuse_past

    if value in (None, ""):
        return None
    try:
        day = DateField().to_internal_value(value)
    except Exception:
        raise drf_serializers.ValidationError(
            {field: ["Use a date like 2026-10-31."]}
        )
    # Every date read here is a deadline — a visit due, the next one due.
    return refuse_past(day, field)


from apps.notifications import service as notices
from common.permissions import CapabilityGate, TechnicianCanCreate, can, can_any, decides_for

from .models import (
    MaintenancePartRequest,
    MaintenanceRecord,
    MaintenanceRecordPhoto,
    MaintenanceSchedule,
    MaintenanceVisit,
)
from .serializers import (
    MaintenanceVisitPhotoSerializer,
    MaintenancePartDecisionSerializer,
    MaintenancePartRequestSerializer,
    MaintenanceRecordPhotoSerializer,
    MaintenanceRecordSerializer,
    MaintenanceScheduleSerializer,
    MaintenanceVisitSerializer,
)

def _answers_for(user, line) -> bool:
    """May this person answer this particular line?

    Being a supervisor is not the same as being *their* supervisor. The
    organogram already records who answers to whom, so a line is answered by
    the asker's own reporting line and nobody else's; whoever acts across
    teams answers for everyone.
    """
    return decides_for(user, line.requested_by, "review_maintenance")


def _may_withdraw(user, line) -> bool:
    """The person who asked, or somebody above them."""
    if line.requested_by_id == user.pk:
        return True
    return _answers_for(user, line)


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
        if view.action == "decide":
            return can(user, "review_maintenance")
        return can_any(user, "work_maintenance", "review_maintenance")


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
        line = serializer.save(
            requested_by=self.request.user,
            visit=schedule.open_visit() if schedule is not None else None,
        )
        # The asker's own supervisor hears about it; so does anyone who
        # answers for every team.
        notices.ask(
            "review_maintenance", exclude=[self.request.user], scope_to=self.request.user,
            kind="request_raised",
            title=f"Parts requested: {line.what}",
            message=f"{line.quantity_requested} {line.unit} for {schedule.title if schedule else 'a job'}",
            link=f"/maintenance?schedule={schedule.pk}" if schedule else "/maintenance",
            ref=f"part:{line.pk}", data={"part_request": str(line.pk)},
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
        notices.resolve(f"part:{instance.pk}")

    @action(detail=True, methods=["post"])
    def decide(self, request, pk=None):
        """Approve this line, for all or part of what was asked, or reject it.

        Approving raises the request the store will issue against; it does not
        move any stock itself.
        """
        from .parts import decide as decide_line

        if not can(request.user, "review_maintenance"):
            return Response(
                {"detail": "Only a supervisor or above can answer a request for parts."},
                status=drf_status.HTTP_403_FORBIDDEN,
            )
        asked = self.get_object()
        # Nobody signs off their own request: a supervisor who needs a part
        # asks the person they report to, the same as anyone. A super admin
        # is the exception by instruction — it has nobody to report to, so
        # the rule would simply stop it releasing anything it asked for.
        # The decision is still recorded against them, so the trail shows
        # who asked and who released it were the same person.
        if asked.requested_by_id == request.user.pk and not lifecycle.is_admin(request.user):
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
        notices.resolve(f"part:{line.pk}")
        approved = ser.validated_data["approve"]
        if line.requested_by_id:
            notices.tell(
                [line.requested_by], exclude=[request.user], kind="request_answered",
                title=f"Parts {'approved' if approved else 'turned down'}: {line.what}",
                message=(f"{line.quantity_approved} released to the store's queue" if approved else (line.decision_note or "")),
                link=f"/maintenance?schedule={line.schedule_id}", data={"part_request": str(line.pk)},
            )
        if approved and line.issuance_request_id:
            req = line.issuance_request
            notices.ask(
                "issue_stock", exclude=[request.user], kind="request_raised",
                title=f"Material requested: {req.what}",
                message=f"{req.quantity_requested} · {req.request_number} · {req.purpose}",
                link="/inventory?tab=requests", ref=f"issue:{req.pk}", data={"request": str(req.pk)},
            )
        return Response(self.get_serializer(line).data)


class MaintenanceScheduleViewSet(RefusesSilentNoOps, viewsets.ModelViewSet):
    # Reading this is a permission, not just a menu entry.
    read_capability = "view_maintenance"
    action_capabilities = {"assign": "assign_maintenance", "cancel": "manage_maintenance", "map_data": "view_maintenance"}
    # Newest first, by the day the work began — which is a different date
    # depending on what kind of job it is. A breakdown began when somebody
    # reported it; a schedule began on the day its rounds start. Ordering
    # by next_due put a job raised this morning below one from last month.
    queryset = (
        MaintenanceSchedule.objects.select_related(
            "device", "site", "assigned_to", "ticket"
        )
        .prefetch_related("vendors", "visits__assigned_to", "visits__photos")
        .annotate(
            began=Coalesce(
                Cast("ticket__created_at", DateField()),
                "start_date",
                Cast("created_at", DateField()),
            )
        )
        .order_by("-began", "-created_at")
    )
    serializer_class = MaintenanceScheduleSerializer
    permission_classes = [IsAuthenticated, TechnicianCanCreate, CapabilityGate]
    filterset_fields = [
        "maintenance_type", "frequency", "status", "is_active", "assigned_to", "device", "priority",
        "ticket",
    ]
    search_fields = ["title", "device__asset_code", "device__display_name"]
    ordering_fields = ["began", "next_due", "created_at", "priority"]

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
        if not can(user, "manage_maintenance"):
            if obj.assigned_to_id not in (user.pk, None):
                raise PermissionDenied("This round is not yours to change.")
        return obj

    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        """Give the work to a technician, with a date. Opens the next visit."""
        from apps.accounts.models import User

        job = self.get_object()
        tech = User.objects.filter(pk=request.data.get("technician")).first()
        due = _as_date(request.data.get("due_date"), "due_date")
        visit = (
            lifecycle.assign(job, user=request.user, technician=tech, due_date=due)
            if job.ticket_id
            else lifecycle.assign_round(job, user=request.user, technician=tech, due_date=due)
        )
        return Response(
            MaintenanceVisitSerializer(visit, context=self.get_serializer_context()).data
        )

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """Called off: the job closes and the asset goes back into service."""
        job = self.get_object()
        lifecycle.cancel(job, user=request.user, reason=request.data.get("reason", "") or "")
        job.refresh_from_db()
        return Response(self.get_serializer(job).data)

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
            return can_any(request.user, "work_maintenance", "review_maintenance")
        if view.action in ("update", "partial_update"):
            return can(request.user, "manage_maintenance")
        # The corrective moves carry their own, finer rules — only the
        # technician this visit was given to may photograph or finish it,
        # only the office may review it — and those need the visit itself
        # to decide, which a has_permission check cannot see.
        if view.action in ("photos", "complete_visit", "review"):
            return True
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

    def _corrective(self, visit):
        """Corrective work answers to the lifecycle; preventive does not."""
        return (
            visit.schedule.maintenance_type == MaintenanceSchedule.MaintenanceType.CORRECTIVE
            and visit.schedule.ticket_id is not None
        )

    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser, FormParser, JSONParser])
    def photos(self, request, pk=None):
        """A photograph of this visit, and what it is a photograph of."""
        from .models import MaintenanceVisitPhoto

        visit = self.get_object()
        if visit.assigned_to_id != request.user.pk and not lifecycle.is_office(request.user):
            return Response(
                {"detail": "Only the technician on this visit, or the office, can add photos."},
                status=drf_status.HTTP_403_FORBIDDEN,
            )
        kind = request.data.get("kind") or MaintenanceVisitPhoto.Kind.OTHER
        if kind not in dict(MaintenanceVisitPhoto.Kind.choices):
            return Response({"kind": ["Say whether it is a before, after or other photo."]}, status=400)
        images = request.FILES.getlist("images") or (
            [request.FILES["image"]] if "image" in request.FILES else []
        )
        if not images:
            return Response({"images": ["Attach at least one photo."]}, status=400)

        def coord(name):
            raw = request.data.get(name)
            return raw if raw not in (None, "", "null") else None

        made = [
            MaintenanceVisitPhoto.objects.create(
                visit=visit, kind=kind, image=image,
                caption=request.data.get("caption", "") or "",
                taken_by=request.user,
                latitude=coord("latitude"), longitude=coord("longitude"),
            )
            for image in images
        ]
        return Response(
            MaintenanceVisitPhotoSerializer(made, many=True, context=self.get_serializer_context()).data,
            status=drf_status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="complete")
    def complete_visit(self, request, pk=None):
        """The technician is done, and says whether the fault is fixed."""
        visit = self.get_object()
        if not self._corrective(visit):
            # A round has no ticket to move, but it is handed in and
            # accepted exactly as a breakdown is.
            visit = lifecycle.complete_round(
                visit, user=request.user,
                remarks=request.data.get("remarks", "") or "",
                settlement=request.data.get("parts_settlement") or None,
            )
            self._ask_for_review(visit, request.user)
            body = self.get_serializer(visit).data
            body["return_grn"] = getattr(visit, "return_grn", None)
            return Response(body)
        resolved = request.data.get("resolved")
        if isinstance(resolved, str):
            resolved = resolved.lower() in ("true", "1", "yes")
        visit = lifecycle.complete(
            visit, user=request.user, resolved=resolved,
            remarks=request.data.get("remarks", "") or "",
            settlement=request.data.get("parts_settlement") or None,
        )
        body = self.get_serializer(visit).data
        # Where the leftovers went, so the technician is told rather than
        # left wondering whether the store has them.
        body["return_grn"] = getattr(visit, "return_grn", None)
        self._ask_for_review(visit, request.user)
        return Response(body)

    def _ask_for_review(self, visit, actor):
        job = visit.schedule
        notices.ask(
            "review_maintenance", exclude=[actor], scope_to=actor,
            title=f"Review needed: {job.title}",
            message=(f"{job.device.asset_code} · " if job.device_id else "") + f"finished by {notices.who(actor)}",
            link=f"/maintenance?schedule={job.pk}", ref=f"visit:{visit.pk}", data={"schedule": str(job.pk)},
        )

    def _answer_review(self, visit, actor, accepted, note=""):
        job = visit.schedule
        notices.resolve(f"visit:{visit.pk}")
        if visit.assigned_to_id:
            notices.tell(
                [visit.assigned_to], exclude=[actor], kind="approval_decided",
                title=f"{job.title}: {'accepted' if accepted else 'another visit needed'}",
                message=note or "", link=f"/maintenance?schedule={job.pk}", data={"schedule": str(job.pk)},
            )

    @action(detail=True, methods=["post"])
    def review(self, request, pk=None):
        """The office decides: accepted as resolved, or another visit."""
        from apps.accounts.models import User

        visit = self.get_object()
        if not decides_for(request.user, visit.assigned_to, "review_maintenance"):
            return Response(
                {"detail": "This visit is reviewed by the technician's own supervisor, or by whoever acts across teams."},
                status=drf_status.HTTP_403_FORBIDDEN,
            )
        if not self._corrective(visit):
            # A round is reviewed the same way a breakdown is: accepted, or
            # sent back for another visit with a reason on the record.
            call = request.data.get("decision") or MaintenanceVisit.Review.ACCEPTED
            if call == MaintenanceVisit.Review.UNRESOLVED:
                tech = User.objects.filter(pk=request.data.get("technician")).first()
                nxt = lifecycle.send_round_back(
                    visit, user=request.user,
                    reason=request.data.get("reason") or "",
                    technician=tech,
                    next_due=_as_date(request.data.get("next_due"), "next_due"),
                    note=request.data.get("note", "") or "",
                )
                self._answer_review(visit, request.user, False, request.data.get("reason") or "")
                return Response(self.get_serializer(nxt).data)
            if call != MaintenanceVisit.Review.ACCEPTED:
                return Response(
                    {"decision": ["Say 'accepted' or 'unresolved'."]},
                    status=drf_status.HTTP_400_BAD_REQUEST,
                )
            visit = lifecycle.accept_round(
                visit, user=request.user,
                note=request.data.get("note", "") or "",
                cost_lines=request.data.get("cost_lines") or None,
                component_prices=request.data.get("component_prices") or None,
            )
            self._answer_review(visit, request.user, True, request.data.get("note", "") or "")
            return Response(self.get_serializer(visit).data)
        decision = request.data.get("decision")
        if decision == MaintenanceVisit.Review.ACCEPTED:
            visit = lifecycle.accept(
                visit, user=request.user, note=request.data.get("note", "") or "",
                cost_lines=request.data.get("cost_lines") or None,
                component_prices=request.data.get("component_prices") or None,
            )
            self._answer_review(visit, request.user, True, request.data.get("note", "") or "")
            return Response(self.get_serializer(visit).data)
        if decision == MaintenanceVisit.Review.UNRESOLVED:
            tech = User.objects.filter(pk=request.data.get("technician")).first()
            nxt = lifecycle.mark_unresolved(
                visit, user=request.user,
                reason=request.data.get("reason") or "",
                technician=tech,
                next_due=_as_date(request.data.get("next_due"), "next_due"),
                note=request.data.get("note", "") or "",
            )
            self._answer_review(visit, request.user, False, request.data.get("reason") or "")
            return Response(self.get_serializer(nxt).data)
        return Response(
            {"decision": ["Say 'accepted' or 'unresolved'."]},
            status=drf_status.HTTP_400_BAD_REQUEST,
        )

    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        """Somebody is on site: the round is under way, and so is the job."""
        from django.utils import timezone

        visit = self.get_object()
        # Where the phone says it is. Offered, never demanded: a basement
        # with no signal is still a place work gets done.
        where = _where_from(request.data)
        # Corrective work goes through the lifecycle, which asks for a
        # BEFORE photo and for the person starting it to be the one it was
        # given to. Preventive rounds carry on exactly as they were.
        if self._corrective(visit):
            return Response(self.get_serializer(
                lifecycle.start(visit, user=request.user, where=where)
            ).data)
        if visit.status != MaintenanceVisit.Status.PLANNED:
            return Response(
                {"detail": f"This round is already {visit.get_status_display().lower()}."},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )
        if visit.assigned_to_id is None:
            return Response(
                {"detail": "Assign a technician before this round starts."},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )
        # The same evidence a breakdown asks for. A round that starts with
        # no photograph of what was found cannot later show what changed.
        if not visit.has_before_photo:
            return Response(
                {"detail": "Upload a BEFORE photo first — it is the record of what was found."},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )
        # Asked for a part? Then it is in hand before anybody sets off.
        waiting = lifecycle.parts_not_yet_in_hand(visit)
        if waiting:
            return Response({"detail": (
                "The store has not handed these over yet: "
                + ", ".join(f"{line.what} ({why})" for line, why in waiting)
                + "."
            )}, status=drf_status.HTTP_400_BAD_REQUEST)
        visit.status = MaintenanceVisit.Status.IN_PROGRESS
        visit.started_at = timezone.now()
        visit.start_latitude, visit.start_longitude = where
        visit.save(update_fields=[
            "status", "started_at", "start_latitude", "start_longitude", "updated_at",
        ])
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
        # Corrective work is finished on its visit: the technician completes
        # it with an after photo and a verdict, the office accepts it, and
        # the record is written from that. Filing a record here walked past
        # all of it and closed a job nobody had even been assigned to.
        job = serializer.validated_data.get("schedule")
        if (
            job is not None
            and job.maintenance_type == MaintenanceSchedule.MaintenanceType.CORRECTIVE
            and job.ticket_id is not None
        ):
            raise drf_serializers.ValidationError({"detail": (
                "Corrective work is completed on its visit, not recorded here. "
                "Open the job and complete the visit."
            )})
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
