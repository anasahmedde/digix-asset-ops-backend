from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import AttendanceRecord
from .serializers import AttendanceRecordSerializer

MANAGER_ROLES = ("super_admin", "group_head", "ops_manager", "supervisor")


class AttendanceRecordViewSet(viewsets.ModelViewSet):
    serializer_class = AttendanceRecordSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["user", "check_type", "site"]
    ordering_fields = ["created_at"]

    def get_queryset(self):
        qs = AttendanceRecord.objects.select_related("user", "site")
        role = getattr(self.request.user, "role", "")
        if role in MANAGER_ROLES or self.request.user.is_superuser:
            return qs.all()
        return qs.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

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
