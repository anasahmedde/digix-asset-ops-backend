from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.permissions import TechnicianCanCreate

from .models import MaintenanceRecord, MaintenanceRecordPhoto, MaintenanceSchedule
from .serializers import (
    MaintenanceRecordPhotoSerializer,
    MaintenanceRecordSerializer,
    MaintenanceScheduleSerializer,
)


class MaintenanceScheduleViewSet(viewsets.ModelViewSet):
    queryset = MaintenanceSchedule.objects.select_related(
        "device", "site", "assigned_to"
    ).all()
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


class MaintenanceRecordPhotoViewSet(viewsets.ModelViewSet):
    queryset = MaintenanceRecordPhoto.objects.select_related("record").all()
    serializer_class = MaintenanceRecordPhotoSerializer
    permission_classes = [IsAuthenticated, TechnicianCanCreate]
    filterset_fields = ["record"]

    def perform_create(self, serializer):
        serializer.save(taken_by=self.request.user)
