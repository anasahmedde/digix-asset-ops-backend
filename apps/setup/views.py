from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from common.permissions import AdminManagerWriteElseRead, IsAdminOrManager

from .models import (
    Company,
    NumberingScheme,
    PaymentTerms,
    TermsTemplate,
    WarrantyPeriodPreset,
)
from .serializers import (
    CompanySerializer,
    NumberingSchemeSerializer,
    PaymentTermsSerializer,
    TermsTemplateSerializer,
    WarrantyPeriodPresetSerializer,
)


class CompanyViewSet(viewsets.ModelViewSet):
    queryset = Company.objects.all()
    serializer_class = CompanySerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    search_fields = ["name", "legal_name"]


class NumberingSchemeViewSet(viewsets.ModelViewSet):
    """Managed by admins/managers; controls auto-generated codes platform-wide."""

    queryset = NumberingScheme.objects.all()
    serializer_class = NumberingSchemeSerializer
    permission_classes = [IsAuthenticated, IsAdminOrManager]
    filterset_fields = ["entity", "is_active"]


class PaymentTermsViewSet(viewsets.ModelViewSet):
    queryset = PaymentTerms.objects.all()
    serializer_class = PaymentTermsSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["is_active"]
    search_fields = ["name", "code"]
    ordering_fields = ["days", "name", "created_at"]


class TermsTemplateViewSet(viewsets.ModelViewSet):
    queryset = TermsTemplate.objects.all()
    serializer_class = TermsTemplateSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["category", "is_active", "is_default"]
    search_fields = ["name", "body"]


class WarrantyPeriodPresetViewSet(viewsets.ModelViewSet):
    queryset = WarrantyPeriodPreset.objects.all()
    serializer_class = WarrantyPeriodPresetSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["is_active"]
    ordering_fields = ["months", "label"]
