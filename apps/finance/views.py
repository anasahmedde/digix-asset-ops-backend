from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from common.permissions import FinanceWriteElseRead

from .models import Invoice, Payment
from .serializers import InvoiceSerializer, PaymentSerializer


class InvoiceViewSet(viewsets.ModelViewSet):
    queryset = (
        Invoice.objects.select_related(
            "client", "supplier", "purchase_order", "created_by"
        )
        .prefetch_related("payments")
        .all()
    )
    serializer_class = InvoiceSerializer
    permission_classes = [IsAuthenticated, FinanceWriteElseRead]
    filterset_fields = ["invoice_type", "status", "client", "supplier"]
    search_fields = ["invoice_number"]
    ordering_fields = ["issue_date", "due_date", "total_amount"]


class PaymentViewSet(viewsets.ModelViewSet):
    queryset = Payment.objects.select_related("invoice", "recorded_by").all()
    serializer_class = PaymentSerializer
    permission_classes = [IsAuthenticated, FinanceWriteElseRead]
    filterset_fields = ["invoice", "method"]
    ordering_fields = ["payment_date"]
