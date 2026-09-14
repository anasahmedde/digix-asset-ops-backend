from django.contrib import admin

from .models import Quotation, QuotationItem


class QuotationItemInline(admin.TabularInline):
    model = QuotationItem
    extra = 0


@admin.register(Quotation)
class QuotationAdmin(admin.ModelAdmin):
    list_display = ["quote_number", "title", "status", "client", "total_amount", "valid_until"]
    list_filter = ["status", "currency"]
    search_fields = ["quote_number", "title", "client__name"]
    readonly_fields = ["quote_number", "total_amount", "accepted_at"]
    inlines = [QuotationItemInline]


@admin.register(QuotationItem)
class QuotationItemAdmin(admin.ModelAdmin):
    list_display = ["quotation", "description", "quantity", "unit_price"]
    search_fields = ["description", "quotation__quote_number"]
