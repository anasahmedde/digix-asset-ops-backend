from django.utils import timezone
from common import clock
from rest_framework import viewsets
from rest_framework.decorators import action
from common.permissions import CapabilityGate, can
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import AttendanceRecord
from .serializers import AttendanceRecordSerializer

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
