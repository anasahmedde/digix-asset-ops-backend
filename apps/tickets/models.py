from django.conf import settings
from django.db import models

from common.models import TimeStampedModel
from common.utils import upload_to_path


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
        PENDING_REVIEW = "pending_review", "Pending Review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        CLOSED = "closed", "Closed"

    class Category(models.TextChoices):
        INSTALLATION = "installation", "Installation"
        REPAIR = "repair", "Repair"
        REPLACEMENT = "replacement", "Replacement"
        INSPECTION = "inspection", "Inspection"
        RELOCATION = "relocation", "Relocation"
        OTHER = "other", "Other"

    title = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OTHER)

    device = models.ForeignKey(
        "assets.Device", on_delete=models.SET_NULL, null=True, blank=True, related_name="tickets"
    )
    site = models.ForeignKey(
        "sites.Site", on_delete=models.SET_NULL, null=True, blank=True, related_name="tickets"
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_tickets"
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

    VALID_TRANSITIONS = {
        Status.OPEN: (Status.IN_PROGRESS,),
        Status.IN_PROGRESS: (Status.ON_HOLD, Status.BLOCKED, Status.PENDING_REVIEW),
        Status.ON_HOLD: (Status.IN_PROGRESS,),
        Status.BLOCKED: (Status.IN_PROGRESS,),
        Status.PENDING_REVIEW: (Status.APPROVED, Status.REJECTED),
        Status.REJECTED: (Status.IN_PROGRESS,),
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
        return f"#{self.id.__str__()[:8]} - {self.title}"

    def can_transition_to(self, new_status: str) -> bool:
        allowed = self.VALID_TRANSITIONS.get(self.status, ())
        return new_status in allowed


class TicketAttachment(TimeStampedModel):
    class AttachmentType(models.TextChoices):
        GENERAL = "general", "General"
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
    content = models.TextField()
    comment_type = models.CharField(
        max_length=20, choices=CommentType.choices, default=CommentType.COMMENT,
    )
    old_status = models.CharField(max_length=20, blank=True)
    new_status = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"Comment on {self.ticket} by {self.author}"
