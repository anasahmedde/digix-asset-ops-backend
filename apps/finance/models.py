from django.conf import settings
from django.db import models

from common.codes import generate_code
from common.models import TimeStampedModel


class Invoice(TimeStampedModel):
    class InvoiceType(models.TextChoices):
        PAYABLE = "payable", "Accounts Payable"
        RECEIVABLE = "receivable", "Accounts Receivable"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent"
        PARTIALLY_PAID = "partially_paid", "Partially Paid"
        PAID = "paid", "Paid"
        OVERDUE = "overdue", "Overdue"
        CANCELLED = "cancelled", "Cancelled"

    class Currency(models.TextChoices):
        PKR = "PKR", "Pakistani Rupee"
        AED = "AED", "UAE Dirham"
        SAR = "SAR", "Saudi Riyal"
        QAR = "QAR", "Qatari Riyal"
        USD = "USD", "US Dollar"
        EUR = "EUR", "Euro"
        GBP = "GBP", "British Pound"

    invoice_number = models.CharField(max_length=50, unique=True, blank=True, db_index=True)
    invoice_type = models.CharField(max_length=15, choices=InvoiceType.choices)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.DRAFT)
    currency = models.CharField(max_length=3, choices=Currency.choices, default=Currency.PKR)
    client = models.ForeignKey(
        "clients.Client", on_delete=models.SET_NULL, null=True, blank=True, related_name="invoices"
    )
    supplier = models.ForeignKey(
        "suppliers.Supplier", on_delete=models.SET_NULL, null=True, blank=True, related_name="invoices"
    )
    purchase_order = models.ForeignKey(
        "procurement.PurchaseOrder", on_delete=models.SET_NULL, null=True, blank=True, related_name="invoices"
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    issue_date = models.DateField()
    due_date = models.DateField()
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="created_invoices"
    )

    class Meta:
        ordering = ["-issue_date"]

    def __str__(self):
        return f"{self.invoice_number} - {self.get_invoice_type_display()}"

    def save(self, *args, **kwargs):
        if not self.invoice_number:
            self.invoice_number = generate_code("invoice", model=type(self), field="invoice_number")
        super().save(*args, **kwargs)

    @property
    def balance_due(self):
        return self.total_amount - self.paid_amount


class Payment(TimeStampedModel):
    class Method(models.TextChoices):
        BANK_TRANSFER = "bank_transfer", "Bank Transfer"
        CHEQUE = "cheque", "Cheque"
        CASH = "cash", "Cash"
        CREDIT_CARD = "credit_card", "Credit Card"
        OTHER = "other", "Other"

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_date = models.DateField()
    method = models.CharField(max_length=15, choices=Method.choices, default=Method.BANK_TRANSFER)
    reference = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="recorded_payments"
    )

    class Meta:
        ordering = ["-payment_date"]

    def __str__(self):
        return f"Payment {self.amount} for {self.invoice.invoice_number}"
