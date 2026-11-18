from decimal import Decimal

from rest_framework import serializers

from common.dates import DateOrder
from common.money import HidesMoney

from .models import Invoice, Payment


class PaymentSerializer(HidesMoney, serializers.ModelSerializer):
    """A payment against exactly one invoice or one purchase order, never
    more than what is still owed on it."""

    recorded_by_name = serializers.CharField(source="recorded_by.get_full_name", read_only=True, default=None)

    class Meta:
        model = Payment
        fields = [
            "id", "invoice", "purchase_order", "kind", "amount", "payment_date",
            "method", "reference", "notes",
            "recorded_by", "recorded_by_name", "created_at",
        ]
        read_only_fields = ["id", "recorded_by", "created_at"]

    def validate(self, attrs):
        from .ledger import ZERO, payable_orders, po_discount, split_payments

        invoice = attrs.get("invoice", getattr(self.instance, "invoice", None))
        order = attrs.get("purchase_order", getattr(self.instance, "purchase_order", None))
        if bool(invoice) == bool(order):
            raise serializers.ValidationError({"detail": "A payment is made against one invoice or one purchase order."})
        amount = attrs.get("amount", getattr(self.instance, "amount", None))
        if amount is None or amount <= 0:
            raise serializers.ValidationError({"amount": "Enter an amount above zero."})
        if invoice:
            if invoice.invoice_type != Invoice.InvoiceType.RECEIVABLE or invoice.status in (
                Invoice.Status.DRAFT, Invoice.Status.CANCELLED,
            ):
                raise serializers.ValidationError({"invoice": "Payments are received only against an approved client invoice."})
            total, others = invoice.total_amount, invoice.payments.all()
        else:
            if not payable_orders().filter(pk=order.pk).exists():
                raise serializers.ValidationError({"purchase_order": "Only an approved purchase order is paid."})
            total, others = order.total_amount - po_discount(order), order.payments.all()
        if self.instance:
            others = others.exclude(pk=self.instance.pk)
        balance = Decimal(total) - split_payments(others)["paid"]
        if amount > max(balance, ZERO):
            raise serializers.ValidationError({"amount": f"Only {max(balance, ZERO):,.2f} is still owed on it."})
        return attrs


class InvoiceSerializer(DateOrder, HidesMoney, serializers.ModelSerializer):
    date_order = (("due_date", "issue_date", "the issue date"),)
    client_name = serializers.CharField(source="client.name", read_only=True, default=None)
    supplier_name = serializers.CharField(source="supplier.name", read_only=True, default=None)
    approved_by_name = serializers.CharField(source="approved_by.get_full_name", read_only=True, default=None)
    quote_number = serializers.CharField(source="quotation.quote_number", read_only=True, default=None)
    po_number = serializers.CharField(source="purchase_order.po_number", read_only=True, default=None)
    balance_due = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    payments = PaymentSerializer(many=True, read_only=True)

    class Meta:
        model = Invoice
        fields = [
            "id", "invoice_number", "invoice_type", "status", "currency",
            "client", "client_name", "supplier", "supplier_name",
            "purchase_order", "po_number", "reference", "quotation", "quote_number", "description",
            "amount", "discount_amount", "tax_amount", "total_amount",
            "issue_date", "due_date", "paid_amount", "notes",
            "created_by", "approved_by", "approved_by_name", "approved_at",
            "balance_due", "payments",
            "created_at", "updated_at",
        ]
        # Status follows approval and the payments; the total is the amount
        # plus tax; what has been paid is what the payments add up to.
        read_only_fields = [
            "id", "status", "total_amount", "paid_amount", "created_by",
            "approved_by", "approved_at", "created_at", "updated_at",
        ]
        # A client invoice unless it is entered against a purchase order,
        # which makes it the supplier's.
        extra_kwargs = {"invoice_type": {"required": False}}

    def validate(self, attrs):
        attrs = super().validate(attrs)
        order = attrs.get("purchase_order", getattr(self.instance, "purchase_order", None))
        # A supplier's invoice is a record of their bill, not ours to approve,
        # so it can be corrected; a client invoice is fixed once approved.
        if self.instance and self.instance.status != Invoice.Status.DRAFT and order is None:
            changed = set(attrs) - {"notes"}
            if changed:
                raise serializers.ValidationError({"detail": "An approved invoice can no longer be changed; only its notes."})
        if self.instance is None:
            attrs.setdefault("invoice_type", Invoice.InvoiceType.RECEIVABLE)
        if order is not None:
            self._bill_order(order, attrs)
            amount = attrs.get("amount", getattr(self.instance, "amount", None))
            tax = attrs.get("tax_amount", getattr(self.instance, "tax_amount", None)) or Decimal("0")
            discount = attrs.get("discount_amount", getattr(self.instance, "discount_amount", None)) or Decimal("0")
            if amount is not None and Decimal(amount) <= 0:
                raise serializers.ValidationError({"amount": "Enter an amount above zero."})
            if Decimal(tax) < 0:
                raise serializers.ValidationError({"tax_amount": "Tax cannot be negative."})
            if discount < 0 or (amount is not None and discount > Decimal(amount)):
                raise serializers.ValidationError({"discount_amount": "The discount is between zero and the amount."})
            if amount is not None:
                attrs["total_amount"] = Decimal(amount) - Decimal(discount) + Decimal(tax)
            return attrs
        attrs.pop("discount_amount", None)
        quotation = attrs.get("quotation", getattr(self.instance, "quotation", None))
        if quotation is not None:
            self._bill_quotation(quotation, attrs)
        kind = attrs.get("invoice_type", getattr(self.instance, "invoice_type", Invoice.InvoiceType.RECEIVABLE))
        if kind == Invoice.InvoiceType.PAYABLE:
            raise serializers.ValidationError({"purchase_order": "A supplier's invoice is entered against its purchase order."})
        if kind == Invoice.InvoiceType.RECEIVABLE and not attrs.get("client", getattr(self.instance, "client", None)):
            raise serializers.ValidationError({"client": "Choose the client this invoice is raised to."})
        amount = attrs.get("amount", getattr(self.instance, "amount", None))
        tax = attrs.get("tax_amount", getattr(self.instance, "tax_amount", None)) or Decimal("0")
        if amount is not None and Decimal(amount) <= 0:
            raise serializers.ValidationError({"amount": "Enter an amount above zero."})
        if Decimal(tax) < 0:
            raise serializers.ValidationError({"tax_amount": "Tax cannot be negative."})
        if amount is not None:
            attrs["total_amount"] = Decimal(amount) + Decimal(tax)
        return attrs

    def _bill_order(self, order, attrs):
        """A supplier's invoice bills the purchase order it is entered
        against: that supplier, in its currency. Going over what is left on
        the order is warned about on screen, not refused - the bill is the
        supplier's and is recorded as it came."""
        from .ledger import payable_orders

        if not payable_orders().filter(pk=order.pk).exists():
            raise serializers.ValidationError({"purchase_order": "Invoices are entered only against an approved purchase order."})
        if attrs.get("quotation"):
            raise serializers.ValidationError({"detail": "An invoice bills a purchase order or a quotation, not both."})
        attrs["invoice_type"] = Invoice.InvoiceType.PAYABLE
        attrs["supplier"] = order.supplier
        attrs["client"] = None
        attrs["currency"] = order.currency
        if not (attrs.get("reference", getattr(self.instance, "reference", "")) or "").strip():
            raise serializers.ValidationError({"reference": "Enter the supplier's invoice number."})

    def _bill_quotation(self, quotation, attrs):
        """An invoice raised from a quotation bills its client, in its
        currency, and never more than is left of it to invoice."""
        from apps.quotations.models import Quotation

        from .ledger import quotation_billing

        if quotation.status != Quotation.Status.ACCEPTED:
            raise serializers.ValidationError({"quotation": "Invoices are raised only against an accepted quotation."})
        attrs["invoice_type"] = Invoice.InvoiceType.RECEIVABLE
        attrs["client"] = quotation.client
        attrs["currency"] = quotation.currency
        amount = attrs.get("amount", getattr(self.instance, "amount", None))
        left = quotation_billing(quotation, exclude=self.instance)["to_invoice"]
        if amount is not None and Decimal(amount) > left:
            raise serializers.ValidationError({"amount": f"Only {quotation.currency} {left:,.2f} of this quotation is left to invoice."})
