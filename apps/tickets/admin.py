from django.contrib import admin

from .models import Ticket, TicketAttachment, TicketComment


class TicketAttachmentInline(admin.TabularInline):
    model = TicketAttachment
    extra = 0
    readonly_fields = ("uploaded_by", "created_at")


class TicketCommentInline(admin.TabularInline):
    model = TicketComment
    extra = 0
    readonly_fields = ("author", "comment_type", "old_status", "new_status", "created_at")


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = (
        "title", "priority", "status", "category",
        "assigned_to", "reported_by", "site", "due_date", "created_at",
    )
    list_filter = ("status", "priority", "category")
    search_fields = ("title", "description")
    raw_id_fields = ("device", "site", "assigned_to", "reported_by", "completed_by", "reviewed_by")
    inlines = [TicketAttachmentInline, TicketCommentInline]


@admin.register(TicketAttachment)
class TicketAttachmentAdmin(admin.ModelAdmin):
    list_display = ("ticket", "uploaded_by", "attachment_type", "caption", "created_at")
    list_filter = ("attachment_type",)
    raw_id_fields = ("ticket", "uploaded_by")


@admin.register(TicketComment)
class TicketCommentAdmin(admin.ModelAdmin):
    list_display = ("ticket", "author", "comment_type", "created_at")
    list_filter = ("comment_type",)
    raw_id_fields = ("ticket", "author")
