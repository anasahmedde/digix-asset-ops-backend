from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from common.permissions import AdminManagerWriteElseRead

from .models import Supplier, SupplierContact, SupplierServiceCategory
from .serializers import (
    SupplierContactSerializer,
    SupplierSerializer,
    SupplierServiceCategorySerializer,
)


class SupplierServiceCategoryViewSet(viewsets.ModelViewSet):
    queryset = SupplierServiceCategory.objects.all()
    serializer_class = SupplierServiceCategorySerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["is_active"]
    search_fields = ["name"]
    ordering_fields = ["name", "created_at"]


class SupplierViewSet(viewsets.ModelViewSet):
    queryset = Supplier.objects.prefetch_related("contacts", "service_categories").all()
    serializer_class = SupplierSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["is_active", "service_categories"]
    search_fields = ["name", "code", "contact_person"]
    ordering_fields = ["name", "created_at"]


class SupplierContactViewSet(viewsets.ModelViewSet):
    queryset = SupplierContact.objects.select_related("supplier").all()
    serializer_class = SupplierContactSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["supplier", "is_primary"]
    search_fields = ["name", "email", "phone"]
