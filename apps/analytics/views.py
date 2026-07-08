from django.db.models import Count
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.permissions import AdminManagerWriteElseRead

from .models import Alert, SavedReport
from .serializers import AlertSerializer, SavedReportSerializer


class AlertViewSet(viewsets.ModelViewSet):
    queryset = Alert.objects.select_related("device", "site").all()
    serializer_class = AlertSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["severity", "category", "is_read", "is_dismissed", "device", "site"]
    search_fields = ["title", "message"]
    ordering_fields = ["created_at", "severity"]

    @action(detail=False, methods=["get"])
    def unread_count(self, request):
        count = Alert.objects.filter(is_read=False, is_dismissed=False).count()
        return Response({"count": count})

    @action(detail=False, methods=["post"])
    def mark_all_read(self, request):
        updated = Alert.objects.filter(is_read=False).update(is_read=True, read_by=request.user)
        return Response({"updated": updated})

    @action(detail=True, methods=["post"])
    def dismiss(self, request, pk=None):
        alert = self.get_object()
        alert.is_dismissed = True
        alert.is_read = True
        alert.read_by = request.user
        alert.save()
        return Response(AlertSerializer(alert).data)


class SavedReportViewSet(viewsets.ModelViewSet):
    queryset = SavedReport.objects.select_related("created_by").all()
    serializer_class = SavedReportSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["report_type", "created_by", "is_scheduled"]
