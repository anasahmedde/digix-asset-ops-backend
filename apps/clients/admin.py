from django.contrib import admin

from .models import Client


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "contact_person", "contact_email", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["name", "code", "contact_person", "contact_email"]
