from django.contrib import admin

from .models import (
    Company,
    NumberingScheme,
    PaymentTerms,
    TermsTemplate,
    WarrantyPeriodPreset,
)


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ["name", "city", "country", "default_currency", "is_primary"]
    list_filter = ["is_primary", "country"]
    search_fields = ["name", "legal_name", "tax_id"]


@admin.register(NumberingScheme)
class NumberingSchemeAdmin(admin.ModelAdmin):
    list_display = ["entity", "prefix", "include_year", "padding", "next_number", "preview", "is_active"]
    list_filter = ["is_active", "include_year"]
    search_fields = ["entity", "prefix"]

    @admin.display(description="Next code")
    def preview(self, obj):
        return obj.preview()


@admin.register(PaymentTerms)
class PaymentTermsAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "days", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["name", "code"]


@admin.register(TermsTemplate)
class TermsTemplateAdmin(admin.ModelAdmin):
    list_display = ["name", "category", "is_default", "is_active"]
    list_filter = ["category", "is_default", "is_active"]
    search_fields = ["name", "body"]


@admin.register(WarrantyPeriodPreset)
class WarrantyPeriodPresetAdmin(admin.ModelAdmin):
    list_display = ["label", "months", "is_active"]
    list_filter = ["is_active"]
