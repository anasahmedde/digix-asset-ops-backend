from django.contrib import admin

from .models import Warranty


@admin.register(Warranty)
class WarrantyAdmin(admin.ModelAdmin):
    list_display = ("device", "warranty_type", "status", "start_date", "end_date", "supplier")
    list_filter = ("warranty_type", "status")
    search_fields = ("reference_number", "coverage_details")
    raw_id_fields = ("device", "supplier")
