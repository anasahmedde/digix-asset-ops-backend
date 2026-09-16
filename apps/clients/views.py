from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from common.permissions import CommercialWriteElseRead

from .models import Client
from .serializers import ClientSerializer


class ClientViewSet(viewsets.ModelViewSet):
    queryset = Client.objects.all()
    serializer_class = ClientSerializer
    permission_classes = [IsAuthenticated, CommercialWriteElseRead]
    filterset_fields = ["is_active"]
    search_fields = ["name", "code", "contact_person"]
    ordering_fields = ["name", "created_at"]
