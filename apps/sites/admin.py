from django.contrib import admin

from .models import DeviceInstallation, InstallationPhoto, Site, SiteZone


class SiteZoneInline(admin.TabularInline):
    model = SiteZone
    extra = 0


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = ["name", "city", "country", "client", "is_active"]
    list_filter = ["city", "country", "is_active"]
    search_fields = ["name", "address"]
    inlines = [SiteZoneInline]


@admin.register(DeviceInstallation)
class DeviceInstallationAdmin(admin.ModelAdmin):
    list_display = ["device", "site", "installed_by", "installed_at"]
    list_filter = ["site"]


@admin.register(InstallationPhoto)
class InstallationPhotoAdmin(admin.ModelAdmin):
    list_display = ["installation", "photo_type", "taken_by", "created_at"]
    list_filter = ["photo_type"]
