from rest_framework import serializers

from .models import Ticket, TicketAttachment, TicketComment, TicketIssueType

MANAGER_ROLES = ("super_admin", "group_head", "ops_manager")


class TicketIssueTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = TicketIssueType
        fields = ["id", "name", "description", "is_active", "sort_order", "created_at"]
        read_only_fields = ["id", "created_at"]


class TicketAttachmentSerializer(serializers.ModelSerializer):
    uploaded_by_name = serializers.CharField(source="uploaded_by.get_full_name", read_only=True, default=None)

    class Meta:
        model = TicketAttachment
        fields = [
            "id", "ticket", "uploaded_by", "uploaded_by_name",
            "file", "caption", "attachment_type", "created_at",
        ]
        read_only_fields = ["id", "ticket", "uploaded_by", "created_at"]


class TicketCommentSerializer(serializers.ModelSerializer):
    author_name = serializers.CharField(source="author.get_full_name", read_only=True, default=None)
    author_avatar = serializers.ImageField(source="author.avatar", read_only=True, default=None)

    class Meta:
        model = TicketComment
        fields = [
            "id", "ticket", "author", "author_name", "author_avatar",
            "content", "image", "comment_type", "old_status", "new_status", "created_at",
        ]
        read_only_fields = ["id", "ticket", "author", "comment_type", "old_status", "new_status", "created_at"]


class _AssignmentGuardMixin:
    """Only operations managers may set or change ticket assignment."""

    def validate(self, attrs):
        request = self.context.get("request")
        if request and ("assigned_to" in attrs or "assigned_vendor" in attrs):
            role = getattr(request.user, "role", "")
            if role not in MANAGER_ROLES:
                raise serializers.ValidationError(
                    {"assigned_to": "Only Operations can assign tickets."}
                )
        return super().validate(attrs)


class TicketSerializer(_AssignmentGuardMixin, serializers.ModelSerializer):
    device_code = serializers.CharField(source="device.asset_code", read_only=True, default=None)
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)
    issue_type_name = serializers.CharField(source="issue_type.name", read_only=True, default=None)
    assigned_to_name = serializers.CharField(source="assigned_to.get_full_name", read_only=True, default=None)
    assigned_vendor_name = serializers.CharField(source="assigned_vendor.name", read_only=True, default=None)
    reported_by_name = serializers.CharField(source="reported_by.get_full_name", read_only=True, default=None)
    completed_by_name = serializers.CharField(source="completed_by.get_full_name", read_only=True, default=None)
    reviewed_by_name = serializers.CharField(source="reviewed_by.get_full_name", read_only=True, default=None)
    attachments = TicketAttachmentSerializer(many=True, read_only=True)
    comments = TicketCommentSerializer(many=True, read_only=True)
    attachment_count = serializers.IntegerField(source="attachments.count", read_only=True)
    comment_count = serializers.IntegerField(source="comments.count", read_only=True)
    is_response_overdue = serializers.BooleanField(read_only=True)
    devices_info = serializers.SerializerMethodField()
    warranty_info = serializers.SerializerMethodField()

    def get_devices_info(self, obj):
        return [
            {"id": str(d.pk), "asset_code": d.asset_code, "display_name": d.display_name}
            for d in obj.devices.all()
        ]

    def get_warranty_info(self, obj):
        w = obj.warranty
        if w is None:
            return None
        return {
            "id": str(w.pk), "warranty_type": w.warranty_type,
            "end_date": w.end_date, "status": w.status,
        }

    def create(self, validated_data):
        from apps.warranties.models import Warranty
        from apps.warranties.services import derive_billability

        devices = validated_data.get("devices") or []
        if not validated_data.get("device") and devices:
            validated_data["device"] = devices[0]

        # Cost liability from the warranty state at creation (WF-14/15) —
        # explicit payload values always win over the derived defaults.
        device = validated_data.get("device")
        category = validated_data.get("category", Ticket.Category.OTHER)
        if device is not None and category in Ticket.WARRANTY_AWARE_CATEGORIES:
            warranty, billable, charge = derive_billability(device)
            if validated_data.get("warranty") is None:
                validated_data["warranty"] = warranty
            if "is_billable" not in validated_data:
                validated_data["is_billable"] = billable
            if not validated_data.get("charge_to"):
                validated_data["charge_to"] = charge

        ticket = super().create(validated_data)
        if ticket.device_id:
            ticket.devices.add(ticket.device)
        if (
            ticket.category == Ticket.Category.WARRANTY_CLAIM
            and ticket.warranty
            and ticket.warranty.status == Warranty.Status.ACTIVE
        ):
            ticket.warranty.status = Warranty.Status.CLAIMED
            ticket.warranty.save(update_fields=["status", "updated_at"])
        return ticket

    class Meta:
        model = Ticket
        fields = [
            "id", "ticket_number", "occurrence", "complaint_by", "title", "description", "priority", "status", "category",
            "issue_type", "issue_type_name",
            "device", "device_code", "devices", "devices_info", "site", "site_name",
            "warranty", "warranty_info", "is_billable", "charge_to", "repair_cost",
            "assigned_to", "assigned_to_name", "assigned_vendor", "assigned_vendor_name",
            "reported_by", "reported_by_name",
            "due_date", "response_due_at", "escalated", "escalated_at", "is_response_overdue",
            "assigned_at", "assignment_escalated", "due_date_escalated",
            "resolved_at", "closed_at", "resolution_notes",
            "completion_notes", "parts_used", "completed_by", "completed_by_name", "completed_at",
            "reviewed_by", "reviewed_by_name", "reviewed_at", "review_comments",
            "blocked_reason", "hold_reason",
            "attachments", "comments", "attachment_count", "comment_count",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "ticket_number", "occurrence", "status",
            "escalated", "escalated_at", "created_at", "updated_at",
            "assigned_at", "assignment_escalated", "due_date_escalated",
            "completed_by", "completed_at", "reviewed_by", "reviewed_at",
            "closed_at",
        ]


class TicketListSerializer(serializers.ModelSerializer):
    """Lighter serializer for list views (no nested comments/attachments)."""

    device_code = serializers.CharField(source="device.asset_code", read_only=True, default=None)
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)
    issue_type_name = serializers.CharField(source="issue_type.name", read_only=True, default=None)
    assigned_to_name = serializers.CharField(source="assigned_to.get_full_name", read_only=True, default=None)
    assigned_vendor_name = serializers.CharField(source="assigned_vendor.name", read_only=True, default=None)
    reported_by_name = serializers.CharField(source="reported_by.get_full_name", read_only=True, default=None)
    attachment_count = serializers.IntegerField(source="attachments.count", read_only=True)
    comment_count = serializers.IntegerField(source="comments.count", read_only=True)
    is_response_overdue = serializers.BooleanField(read_only=True)

    class Meta:
        model = Ticket
        fields = [
            "id", "ticket_number", "occurrence", "complaint_by", "title", "description", "priority", "status", "category",
            "issue_type", "issue_type_name",
            "device", "device_code", "site", "site_name",
            "is_billable", "charge_to",
            "assigned_to", "assigned_to_name", "assigned_vendor", "assigned_vendor_name",
            "reported_by", "reported_by_name",
            "due_date", "response_due_at", "escalated", "is_response_overdue",
            "assignment_escalated", "due_date_escalated",
            "resolved_at", "closed_at", "completion_notes", "parts_used", "completed_at",
            "reviewed_at", "review_comments", "blocked_reason", "hold_reason",
            "attachment_count", "comment_count",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "ticket_number", "occurrence", "status",
            "assignment_escalated", "due_date_escalated",
            "closed_at", "created_at", "updated_at",
        ]


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


class TicketAssignSerializer(serializers.Serializer):
    assigned_to = serializers.UUIDField(required=False, allow_null=True)
    assigned_vendor = serializers.UUIDField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if "assigned_to" not in attrs and "assigned_vendor" not in attrs:
            raise serializers.ValidationError("Provide assigned_to and/or assigned_vendor.")
        return attrs


class TicketSubmitCompletionSerializer(serializers.Serializer):
    completion_notes = serializers.CharField()
    parts_used = serializers.CharField(required=False, allow_blank=True, default="")
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
