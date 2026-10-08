from common.scoping import for_client
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from common.permissions import CapabilityGate

from .models import Client
from .serializers import ClientSerializer


class ClientViewSet(viewsets.ModelViewSet):
    # Reading this is a permission, not just a menu entry.
    read_capability = "view_clients"
    queryset = Client.objects.all()

    def get_queryset(self):
        # A client portal login sees its own record, nobody else's.
        return for_client(super().get_queryset(), self.request.user, "id")
    serializer_class = ClientSerializer
    permission_classes = [IsAuthenticated, CapabilityGate]
    write_capability = "manage_clients"
    filterset_fields = ["is_active"]
    search_fields = ["name", "code", "contact_person"]
    ordering_fields = ["name", "created_at"]
