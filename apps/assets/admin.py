from django.contrib import admin

from .models import AssetCode, Brand, Device, DeviceLifecycleEvent, DeviceModel, MaterialType


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ["name", "is_active", "created_at"]
    search_fields = ["name"]


@admin.register(DeviceModel)
class DeviceModelAdmin(admin.ModelAdmin):
    list_display = ["name", "brand", "model_number", "is_active"]
    list_filter = ["brand", "is_active"]
    search_fields = ["name", "model_number"]


@admin.register(MaterialType)
class MaterialTypeAdmin(admin.ModelAdmin):
    list_display = ["name", "category", "unit"]
    list_filter = ["category"]
    search_fields = ["name"]


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ["asset_code", "serial_number", "device_model", "status", "current_site"]
    list_filter = ["status", "device_model__brand"]
    search_fields = ["asset_code", "serial_number", "mobile_id"]
    readonly_fields = ["asset_code"]


@admin.register(DeviceLifecycleEvent)
class DeviceLifecycleEventAdmin(admin.ModelAdmin):
    list_display = ["device", "event_type", "performed_by", "created_at"]
    list_filter = ["event_type"]


@admin.register(AssetCode)
class AssetCodeAdmin(admin.ModelAdmin):
    list_display = ["device", "format", "is_current", "printed_at"]
    list_filter = ["format", "is_current"]
