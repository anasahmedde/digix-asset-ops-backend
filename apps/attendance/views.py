from django.utils import timezone
from common import clock
from rest_framework import mixins, status as http, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.decorators import action
from common.permissions import CapabilityGate, can
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import AttendanceRecord, DutyRecord
from .serializers import AttendanceRecordSerializer, DutyRecordSerializer

class AttendanceRecordViewSet(viewsets.ModelViewSet):
    # Reading this is a permission, not just a menu entry.
    read_capability = "view_attendance"
    serializer_class = AttendanceRecordSerializer
    permission_classes = [IsAuthenticated, CapabilityGate]
    # Anyone may clock themselves in, and read back whether they are in;
    # seeing the register is view_attendance, correcting other people's
    # records is manage_attendance. The field app checks a technician in and
    # then reads `status`: with that read behind view_attendance, which the
    # technician role does not carry, every check-in was saved and then
    # answered "You do not have permission to perform this action."
    action_capabilities = {"create": None, "status": None}
    write_capability = "manage_attendance"
    filterset_fields = ["user", "check_type", "site"]
    ordering_fields = ["created_at"]

    def get_queryset(self):
        qs = AttendanceRecord.objects.select_related("user", "site")
        user = self.request.user
        if user.is_superuser or can(user, "manage_attendance") or can(user, "act_across_teams"):
            return qs.all()
        # A lead sees their own line's register; everybody sees their own.
        from django.db.models import Q

        return qs.filter(Q(user=user) | Q(user__reports_to=user))

    def perform_create(self, serializer):
        record = serializer.save(user=self.request.user)
        if clock.offline():
            # Sent from the phone's outbox: the record is dated when it
            # happened, and says it arrived later.
            when, arrived = clock.now(), timezone.now()
            AttendanceRecord.objects.filter(pk=record.pk).update(
                created_at=when, recorded_offline=True, uploaded_at=arrived,
            )
            record.created_at, record.recorded_offline, record.uploaded_at = when, True, arrived

    @action(detail=False, methods=["get"])
    def status(self, request):
        """The current user's latest check state."""
        last = AttendanceRecord.objects.filter(user=request.user).order_by("-created_at").first()
        return Response({
            "checked_in": bool(last and last.check_type == AttendanceRecord.CheckType.CHECK_IN),
            "last": AttendanceRecordSerializer(last).data if last else None,
        })

    @action(detail=False, methods=["get"])
    def today(self, request):
        """Records for today (scoped like the list) + who is currently checked in."""
        start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        qs = self.get_queryset().filter(created_at__gte=start)
        # currently checked-in = users whose latest record overall is a check_in
        checked_in_users = []
        seen = set()
        for rec in self.get_queryset().order_by("-created_at"):
            if rec.user_id in seen:
                continue
            seen.add(rec.user_id)
            if rec.check_type == AttendanceRecord.CheckType.CHECK_IN:
                checked_in_users.append(rec.user_id)
        return Response({
            "count": qs.count(),
            "currently_in": len(checked_in_users),
            "records": AttendanceRecordSerializer(qs, many=True).data,
        })


class DutyRecordViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet):
    """Time in / Time out from the field app.

    Anyone may time themselves in and out and ask whether they are timed in;
    that answer carries no place. The register - when and where - is read by
    the person's supervisor (whoever they report to) and by the office. Not
    by the person themselves.
    """

    read_capability = "view_attendance"
    action_capabilities = {"create": None, "status": None}
    serializer_class = DutyRecordSerializer
    permission_classes = [IsAuthenticated, CapabilityGate]
    filterset_fields = ["user", "kind"]
    ordering_fields = ["at"]

    def get_queryset(self):
        qs = DutyRecord.objects.select_related("user")
        user = self.request.user
        if user.is_superuser or can(user, "manage_attendance") or can(user, "act_across_teams"):
            return qs.all()
        return qs.filter(user__reports_to=user)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        kind = serializer.validated_data["kind"]
        when = clock.now()
        last = DutyRecord.objects.filter(user=request.user, at__lte=when).order_by("-at").first()
        on_duty = bool(last and last.kind == DutyRecord.Kind.TIME_IN)
        if kind == DutyRecord.Kind.TIME_IN and on_duty:
            raise ValidationError({"detail": "You are already timed in."})
        if kind == DutyRecord.Kind.TIME_OUT and not on_duty:
            raise ValidationError({"detail": "You have not timed in."})
        offline = clock.offline()
        record = serializer.save(
            user=request.user, at=when, recorded_offline=offline,
            uploaded_at=timezone.now() if offline else None,
        )
        # The person is told it is done, not where they were.
        return Response({"id": str(record.pk), "kind": record.kind, "at": record.at}, status=http.HTTP_201_CREATED)

    @action(detail=False, methods=["get"])
    def status(self, request):
        """Whether the current user is timed in, and since when. No place."""
        last = DutyRecord.objects.filter(user=request.user, at__lte=clock.now()).order_by("-at").first()
        on_duty = bool(last and last.kind == DutyRecord.Kind.TIME_IN)
        return Response({"on_duty": on_duty, "since": last.at if on_duty else None})
