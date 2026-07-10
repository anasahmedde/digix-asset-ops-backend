from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from common.codes import generate_code
from common.models import TimeStampedModel
from common.utils import upload_to_path


class TicketIssueType(TimeStampedModel):
    """Data-driven fault catalogue for tickets (e.g. Module Burnt, HDMI Cable
    Issue). Managed from Setup so new fault types can be added without code."""

    name = models.CharField(max_length=150, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class Ticket(TimeStampedModel):
    class Priority(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"
        CRITICAL = "critical", "Critical"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In Progress"
        ON_HOLD = "on_hold", "On Hold"
        BLOCKED = "blocked", "Blocked"
        ALIGNMENT_PENDING = "alignment_pending", "Alignment Pending"
        PENDING_OPS_APPROVAL = "pending_ops_approval", "Pending Ops Approval"
        PENDING_CLIENT_APPROVAL = "pending_client_approval", "Pending Client Approval"
        PENDING_REVIEW = "pending_review", "Pending Review"
        APPROVED = "approved", "Resolved (Ops Approved)"
        REJECTED = "rejected", "Rejected"
        CLOSED = "closed", "Closed"

    class Category(models.TextChoices):
        INSTALLATION = "installation", "Installation"
        REPAIR = "repair", "Repair"
        REPLACEMENT = "replacement", "Replacement"
        INSPECTION = "inspection", "Inspection"
        RELOCATION = "relocation", "Relocation"
        OTHER = "other", "Other"

    # Response SLA per priority — a ticket still "open" past this window is
    # auto-escalated (see tasks.escalate_overdue_tickets).
    RESPONSE_SLA_HOURS = {"critical": 4, "high": 8, "medium": 24, "low": 48}

    ticket_number = models.CharField(max_length=50, unique=True, blank=True, db_index=True)
    title = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.OPEN)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OTHER)
    issue_type = models.ForeignKey(
        TicketIssueType, on_delete=models.SET_NULL, null=True, blank=True, related_name="tickets"
    )
    # Nth ticket ever raised against this device (1-based); 0 when no device.
    occurrence = models.PositiveIntegerField(default=0)
    # Who complained — free text: client company, client's staff, or internal person.
    complaint_by = models.CharField(max_length=200, blank=True)

    device = models.ForeignKey(
        "assets.Device", on_delete=models.SET_NULL, null=True, blank=True, related_name="tickets"
    )
    site = models.ForeignKey(
        "sites.Site", on_delete=models.SET_NULL, null=True, blank=True, related_name="tickets"
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_tickets"
    )
    # In-warranty assets can be assigned to the vendor instead of / alongside staff.
    assigned_vendor = models.ForeignKey(
        "suppliers.Supplier", on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_tickets"
    )
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="reported_tickets"
    )

    due_date = models.DateField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_notes = models.TextField(blank=True)

    # Completion / approval workflow
    completion_notes = models.TextField(blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="completed_tickets",
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="reviewed_tickets",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_comments = models.TextField(blank=True)

    blocked_reason = models.TextField(blank=True)
    hold_reason = models.TextField(blank=True)

    # Rectification record (filled by the technician/vendor on completion).
    parts_used = models.TextField(blank=True, help_text="Parts consumed during rectification")

    # Response SLA / escalation
    response_due_at = models.DateTimeField(null=True, blank=True)
    escalated = models.BooleanField(default=False)
    escalated_at = models.DateTimeField(null=True, blank=True)

    VALID_TRANSITIONS = {
        Status.OPEN: (Status.IN_PROGRESS, Status.CLOSED),
        Status.IN_PROGRESS: (
            Status.ON_HOLD, Status.BLOCKED, Status.ALIGNMENT_PENDING,
            Status.PENDING_OPS_APPROVAL, Status.PENDING_REVIEW, Status.CLOSED,
        ),
        Status.ON_HOLD: (Status.IN_PROGRESS, Status.CLOSED),
        Status.BLOCKED: (Status.IN_PROGRESS, Status.CLOSED),
        Status.ALIGNMENT_PENDING: (Status.IN_PROGRESS, Status.PENDING_REVIEW, Status.CLOSED),
        # Ops decide: approve rectification (back to work), need client approval,
        # or decline (hold).
        Status.PENDING_OPS_APPROVAL: (
            Status.IN_PROGRESS, Status.PENDING_CLIENT_APPROVAL, Status.ON_HOLD,
        ),
        # Marketing relay the client's decision: approved (back to work) or
        # declined (hold — may later be closed).
        Status.PENDING_CLIENT_APPROVAL: (Status.IN_PROGRESS, Status.ON_HOLD),
        Status.PENDING_REVIEW: (Status.APPROVED, Status.REJECTED),
        Status.REJECTED: (Status.IN_PROGRESS, Status.PENDING_REVIEW),
        Status.APPROVED: (Status.CLOSED,),
        Status.CLOSED: (),
    }

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["priority"]),
            models.Index(fields=["assigned_to", "status"]),
        ]

    def __str__(self):
        return f"{self.ticket_number or str(self.id)[:8]} - {self.title}"

    def save(self, *args, **kwargs):
        if not self.ticket_number:
            self.ticket_number = generate_code("ticket", model=type(self), field="ticket_number")
        if self._state.adding:
            if self.device_id and not self.occurrence:
                self.occurrence = Ticket.objects.filter(device_id=self.device_id).count() + 1
            if not self.response_due_at:
                hours = self.RESPONSE_SLA_HOURS.get(self.priority, 24)
                self.response_due_at = timezone.now() + timedelta(hours=hours)
        super().save(*args, **kwargs)

    @property
    def is_response_overdue(self) -> bool:
        """True while the ticket sits unattended past its response SLA."""
        return bool(
            self.status == self.Status.OPEN
            and self.response_due_at
            and timezone.now() > self.response_due_at
        )

    def can_transition_to(self, new_status: str) -> bool:
        allowed = self.VALID_TRANSITIONS.get(self.status, ())
        return new_status in allowed


class TicketAttachment(TimeStampedModel):
    class AttachmentType(models.TextChoices):
        GENERAL = "general", "General"
        FAULT = "fault", "Fault Evidence"
        COMPLETION = "completion", "Completion Evidence"
        REVIEW = "review", "Review Attachment"

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="attachments")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="ticket_attachments"
    )
    file = models.ImageField(upload_to=upload_to_path)
    caption = models.CharField(max_length=300, blank=True)
    attachment_type = models.CharField(
        max_length=20, choices=AttachmentType.choices, default=AttachmentType.GENERAL,
    )

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"Attachment for {self.ticket} by {self.uploaded_by}"


class TicketComment(TimeStampedModel):
    class CommentType(models.TextChoices):
        COMMENT = "comment", "Comment"
        STATUS_CHANGE = "status_change", "Status Change"
        COMPLETION = "completion", "Completion Submission"
        APPROVAL = "approval", "Approval"
        REJECTION = "rejection", "Rejection"

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="ticket_comments"
    )
    content = models.TextField(blank=True)
    image = models.ImageField(upload_to=upload_to_path, blank=True, help_text="Optional photo attached to the comment")
    comment_type = models.CharField(
        max_length=20, choices=CommentType.choices, default=CommentType.COMMENT,
    )
    old_status = models.CharField(max_length=30, blank=True)
    new_status = models.CharField(max_length=30, blank=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"Comment on {self.ticket} by {self.author}"
