from django.contrib import admin

from .models import ChatMessage, ChatRoom, ChatRoomMembership


class ChatRoomMembershipInline(admin.TabularInline):
    model = ChatRoomMembership
    extra = 0


@admin.register(ChatRoom)
class ChatRoomAdmin(admin.ModelAdmin):
    list_display = ["__str__", "room_type", "is_active", "created_at"]
    list_filter = ["room_type", "is_active"]
    search_fields = ["name"]
    inlines = [ChatRoomMembershipInline]


@admin.register(ChatRoomMembership)
class ChatRoomMembershipAdmin(admin.ModelAdmin):
    list_display = ["room", "user", "last_read_at", "is_muted", "created_at"]
    list_filter = ["is_muted"]


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ["sender", "room", "message_type", "is_edited", "created_at"]
    list_filter = ["message_type", "is_edited"]
    search_fields = ["content"]
