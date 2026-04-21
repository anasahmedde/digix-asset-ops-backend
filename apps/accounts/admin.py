from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import AuditLog, CredentialVault, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ["username", "email", "role", "is_field_staff", "is_active"]
    list_filter = ["role", "is_field_staff", "is_active"]
    fieldsets = BaseUserAdmin.fieldsets + (
        ("DIGIX", {"fields": ("role", "phone", "avatar", "is_field_staff")}),
    )


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ["user", "action", "resource_type", "created_at"]
    list_filter = ["action", "resource_type"]
    readonly_fields = ["id", "user", "action", "resource_type", "resource_id", "detail", "ip_address", "created_at"]


@admin.register(CredentialVault)
class CredentialVaultAdmin(admin.ModelAdmin):
    list_display = ["label", "device", "created_by", "last_rotated"]
    list_filter = ["last_rotated"]
