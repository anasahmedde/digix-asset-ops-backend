from django.contrib import admin

from .models import Invoice, Payment


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("invoice_number", "invoice_type", "status", "total_amount", "issue_date", "due_date")
    list_filter = ("invoice_type", "status")
    search_fields = ("invoice_number",)
    raw_id_fields = ("client", "supplier", "purchase_order", "created_by")
    inlines = [PaymentInline]


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("invoice", "amount", "payment_date", "method")
    list_filter = ("method",)
    raw_id_fields = ("invoice", "recorded_by")
