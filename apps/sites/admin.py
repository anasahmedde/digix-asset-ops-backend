from django.contrib import admin

from .models import (
    DeviceInstallation,
    InstallationDelay,
    InstallationPhoto,
    InstallationStep,
    Site,
    SiteContact,
    SiteZone,
)


class SiteZoneInline(admin.TabularInline):
    model = SiteZone
    extra = 0


class SiteContactInline(admin.TabularInline):
    model = SiteContact
    extra = 0


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = ["name", "city", "state_province", "country", "client", "is_active"]
    list_filter = ["city", "state_province", "country", "is_active"]
    search_fields = ["name", "address"]
    inlines = [SiteContactInline, SiteZoneInline]


@admin.register(SiteContact)
class SiteContactAdmin(admin.ModelAdmin):
    list_display = ["name", "site", "designation", "phone", "is_primary"]
    list_filter = ["is_primary"]
    search_fields = ["name", "email", "phone"]


class InstallationStepInline(admin.TabularInline):
    model = InstallationStep
    extra = 0
    ordering = ["step_number"]


@admin.register(DeviceInstallation)
class DeviceInstallationAdmin(admin.ModelAdmin):
    list_display = ["device", "site", "installed_by", "installed_at", "due_date", "completed_at"]
    list_filter = ["site"]
    inlines = [InstallationStepInline]


@admin.register(InstallationDelay)
class InstallationDelayAdmin(admin.ModelAdmin):
    list_display = ["installation", "step", "cause", "reported_by", "created_at", "resolved_at"]
    list_filter = ["cause"]


@admin.register(InstallationStep)
class InstallationStepAdmin(admin.ModelAdmin):
    list_display = ["installation", "step_number", "step_type", "status", "assigned_team"]
    list_filter = ["step_type", "status"]


@admin.register(InstallationPhoto)
class InstallationPhotoAdmin(admin.ModelAdmin):
    list_display = ["installation", "photo_type", "taken_by", "created_at"]
    list_filter = ["photo_type"]
