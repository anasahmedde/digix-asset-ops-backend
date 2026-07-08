from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from common.permissions import AdminManagerWriteElseRead

from .models import Warranty
from .serializers import WarrantySerializer


class WarrantyViewSet(viewsets.ModelViewSet):
    queryset = Warranty.objects.select_related("device", "supplier").all()
    serializer_class = WarrantySerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["status", "warranty_type", "device", "supplier"]
    search_fields = ["reference_number", "coverage_details"]
    ordering_fields = ["end_date", "start_date"]
