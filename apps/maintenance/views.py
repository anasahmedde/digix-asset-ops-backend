from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from common.permissions import TechnicianCanCreate

from .models import MaintenanceRecord, MaintenanceSchedule
from .serializers import MaintenanceRecordSerializer, MaintenanceScheduleSerializer


class MaintenanceScheduleViewSet(viewsets.ModelViewSet):
    queryset = MaintenanceSchedule.objects.select_related(
        "device", "site", "assigned_to"
    ).all()
    serializer_class = MaintenanceScheduleSerializer
    permission_classes = [IsAuthenticated, TechnicianCanCreate]
    filterset_fields = [
        "maintenance_type", "frequency", "is_active", "assigned_to",
    ]
    search_fields = ["title"]
    ordering_fields = ["next_due", "created_at"]


class MaintenanceRecordViewSet(viewsets.ModelViewSet):
    queryset = MaintenanceRecord.objects.select_related(
        "schedule", "performed_by"
    ).all()
    serializer_class = MaintenanceRecordSerializer
    permission_classes = [IsAuthenticated, TechnicianCanCreate]
    filterset_fields = ["schedule", "status", "performed_by"]
    ordering_fields = ["performed_at"]
