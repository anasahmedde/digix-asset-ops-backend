from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from common.permissions import TechnicianCanCreate

from .models import Ticket
from .serializers import TicketSerializer


class TicketViewSet(viewsets.ModelViewSet):
    queryset = Ticket.objects.select_related(
        "device", "site", "assigned_to", "reported_by"
    ).all()
    serializer_class = TicketSerializer
    permission_classes = [IsAuthenticated, TechnicianCanCreate]
    filterset_fields = [
        "status", "priority", "category", "assigned_to", "site", "device",
    ]
    search_fields = ["title", "description"]
    ordering_fields = ["created_at", "due_date", "priority", "status"]

    def perform_create(self, serializer):
        serializer.save(reported_by=self.request.user)
