from django.contrib import admin

from .models import Supplier, SupplierContact, SupplierServiceCategory


@admin.register(SupplierServiceCategory)
class SupplierServiceCategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["name"]


class SupplierContactInline(admin.TabularInline):
    model = SupplierContact
    extra = 0


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "contact_person", "contact_email", "is_active"]
    list_filter = ["is_active", "service_categories"]
    search_fields = ["name", "code", "contact_person", "contact_email"]
    filter_horizontal = ["service_categories"]
    readonly_fields = ["code"]
    inlines = [SupplierContactInline]


@admin.register(SupplierContact)
class SupplierContactAdmin(admin.ModelAdmin):
    list_display = ["name", "supplier", "designation", "phone", "is_primary"]
    list_filter = ["is_primary"]
    search_fields = ["name", "email", "phone"]
