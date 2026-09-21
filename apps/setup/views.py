from rest_framework import status, viewsets
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from common.permissions import CommercialWriteElseRead, IsAdminOrManager

from .models import (
    EscalationPolicy,
    Company,
    NumberingScheme,
    PaymentTerms,
    TermsTemplate,
    UnitOfMeasure,
    WarrantyPeriodPreset,
)
from .serializers import (
    EscalationPolicySerializer,
    CompanySerializer,
    NumberingSchemeSerializer,
    PaymentTermsSerializer,
    TermsTemplateSerializer,
    UnitOfMeasureSerializer,
    WarrantyPeriodPresetSerializer,
)


class CompanyViewSet(viewsets.ModelViewSet):
    queryset = Company.objects.all()
    serializer_class = CompanySerializer
    permission_classes = [IsAuthenticated, CommercialWriteElseRead]
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
    permission_classes = [IsAuthenticated, CommercialWriteElseRead]
    filterset_fields = ["is_active"]
    search_fields = ["name", "code"]
    ordering_fields = ["days", "name", "created_at"]


class UnitOfMeasureViewSet(viewsets.ModelViewSet):
    """Units components are counted in. Deleting one that is in use would
    leave components counting in a unit nobody can pick again, so those are
    deactivated instead."""

    queryset = UnitOfMeasure.objects.all()
    serializer_class = UnitOfMeasureSerializer
    permission_classes = [IsAuthenticated, CommercialWriteElseRead]
    filterset_fields = ["is_active"]
    search_fields = ["name", "symbol"]
    ordering_fields = ["name", "created_at"]

    def destroy(self, request, *args, **kwargs):
        unit = self.get_object()
        used = unit.usage_count()
        if used:
            return Response(
                {"detail": f"'{unit.name}' is used by {used} component definition(s). Mark it inactive instead of deleting it."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().destroy(request, *args, **kwargs)


class TermsTemplateViewSet(viewsets.ModelViewSet):
    queryset = TermsTemplate.objects.all()
    serializer_class = TermsTemplateSerializer
    permission_classes = [IsAuthenticated, CommercialWriteElseRead]
    filterset_fields = ["category", "is_active", "is_default"]
    search_fields = ["name", "body"]


class WarrantyPeriodPresetViewSet(viewsets.ModelViewSet):
    queryset = WarrantyPeriodPreset.objects.all()
    serializer_class = WarrantyPeriodPresetSerializer
    permission_classes = [IsAuthenticated, CommercialWriteElseRead]
    filterset_fields = ["is_active"]
    ordering_fields = ["months", "label"]


class EscalationPolicyViewSet(viewsets.ModelViewSet):
    queryset = EscalationPolicy.objects.all()
    serializer_class = EscalationPolicySerializer
    permission_classes = [IsAuthenticated, CommercialWriteElseRead]
    filterset_fields = ["is_active", "trigger", "scope", "stage"]
