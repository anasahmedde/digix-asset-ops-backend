from rest_framework import serializers

from .models import Ticket, TicketAttachment, TicketComment


class TicketAttachmentSerializer(serializers.ModelSerializer):
    uploaded_by_name = serializers.CharField(source="uploaded_by.get_full_name", read_only=True, default=None)

    class Meta:
        model = TicketAttachment
        fields = [
            "id", "ticket", "uploaded_by", "uploaded_by_name",
            "file", "caption", "attachment_type", "created_at",
        ]
        read_only_fields = ["id", "uploaded_by", "created_at"]


class TicketCommentSerializer(serializers.ModelSerializer):
    author_name = serializers.CharField(source="author.get_full_name", read_only=True, default=None)
    author_avatar = serializers.ImageField(source="author.avatar", read_only=True, default=None)

    class Meta:
        model = TicketComment
        fields = [
            "id", "ticket", "author", "author_name", "author_avatar",
            "content", "comment_type", "old_status", "new_status", "created_at",
        ]
        read_only_fields = ["id", "author", "comment_type", "old_status", "new_status", "created_at"]


class TicketSerializer(serializers.ModelSerializer):
    device_code = serializers.CharField(source="device.asset_code", read_only=True, default=None)
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)
    assigned_to_name = serializers.CharField(source="assigned_to.get_full_name", read_only=True, default=None)
    reported_by_name = serializers.CharField(source="reported_by.get_full_name", read_only=True, default=None)
    completed_by_name = serializers.CharField(source="completed_by.get_full_name", read_only=True, default=None)
    reviewed_by_name = serializers.CharField(source="reviewed_by.get_full_name", read_only=True, default=None)
    attachments = TicketAttachmentSerializer(many=True, read_only=True)
    comments = TicketCommentSerializer(many=True, read_only=True)
    attachment_count = serializers.IntegerField(source="attachments.count", read_only=True)
    comment_count = serializers.IntegerField(source="comments.count", read_only=True)

    class Meta:
        model = Ticket
        fields = [
            "id", "title", "description", "priority", "status", "category",
            "device", "device_code", "site", "site_name",
            "assigned_to", "assigned_to_name", "reported_by", "reported_by_name",
            "due_date", "resolved_at", "resolution_notes",
            "completion_notes", "completed_by", "completed_by_name", "completed_at",
            "reviewed_by", "reviewed_by_name", "reviewed_at", "review_comments",
            "blocked_reason", "hold_reason",
            "attachments", "comments", "attachment_count", "comment_count",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "created_at", "updated_at",
            "completed_by", "completed_at", "reviewed_by", "reviewed_at",
        ]


class TicketListSerializer(serializers.ModelSerializer):
    """Lighter serializer for list views (no nested comments/attachments)."""

    device_code = serializers.CharField(source="device.asset_code", read_only=True, default=None)
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)
    assigned_to_name = serializers.CharField(source="assigned_to.get_full_name", read_only=True, default=None)
    reported_by_name = serializers.CharField(source="reported_by.get_full_name", read_only=True, default=None)
    attachment_count = serializers.IntegerField(source="attachments.count", read_only=True)
    comment_count = serializers.IntegerField(source="comments.count", read_only=True)

    class Meta:
        model = Ticket
        fields = [
            "id", "title", "description", "priority", "status", "category",
            "device", "device_code", "site", "site_name",
            "assigned_to", "assigned_to_name", "reported_by", "reported_by_name",
            "due_date", "resolved_at", "completion_notes", "completed_at",
            "reviewed_at", "review_comments", "blocked_reason", "hold_reason",
            "attachment_count", "comment_count",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class TicketTransitionSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=Ticket.Status.choices)
    notes = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_status(self, value):
        ticket = self.context["ticket"]
        if not ticket.can_transition_to(value):
            current = ticket.get_status_display()
            target = dict(Ticket.Status.choices).get(value, value)
            raise serializers.ValidationError(
                f"Cannot transition from '{current}' to '{target}'."
            )
        return value


class TicketSubmitCompletionSerializer(serializers.Serializer):
    completion_notes = serializers.CharField()
    images = serializers.ListField(
        child=serializers.ImageField(), required=False, default=list,
    )
    captions = serializers.ListField(
        child=serializers.CharField(allow_blank=True), required=False, default=list,
    )


class TicketReviewSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["approve", "reject"])
    comments = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if attrs["action"] == "reject" and not attrs.get("comments", "").strip():
            raise serializers.ValidationError(
                {"comments": "Comments are required when rejecting."}
            )
        return attrs
