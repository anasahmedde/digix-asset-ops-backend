from rest_framework import serializers

from .models import Invoice, Payment


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = [
            "id", "invoice", "amount", "payment_date",
            "method", "reference", "notes",
            "recorded_by", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class InvoiceSerializer(serializers.ModelSerializer):
    client_name = serializers.CharField(source="client.name", read_only=True, default=None)
    supplier_name = serializers.CharField(source="supplier.name", read_only=True, default=None)
    balance_due = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    payments = PaymentSerializer(many=True, read_only=True)

    class Meta:
        model = Invoice
        fields = [
            "id", "invoice_number", "invoice_type", "status", "currency",
            "client", "client_name", "supplier", "supplier_name",
            "purchase_order", "amount", "tax_amount", "total_amount",
            "issue_date", "due_date", "paid_amount", "notes",
            "created_by", "balance_due", "payments",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
