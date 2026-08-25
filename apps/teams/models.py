from django.conf import settings
from django.db import models

from common.models import TimeStampedModel
from common.utils import upload_to_path


class Project(TimeStampedModel):
    class Status(models.TextChoices):
        PLANNING = "planning", "Planning"
        ON_TRACK = "on_track", "On Track"
        AT_RISK = "at_risk", "At Risk"
        DELAYED = "delayed", "Delayed"
        COMPLETED = "completed", "Completed"
        ON_HOLD = "on_hold", "On Hold"

    class Phase(models.TextChoices):
        """Commercial lifecycle of a client order, from enquiry to retirement."""

        QUERY = "query", "Query"
        ON_HOLD = "on_hold", "On Hold"
        QUOTATION = "quotation", "Quotation"
        NEGOTIATION = "negotiation", "Negotiation"
        ORDER_CONFIRMATION = "order_confirmation", "Order Confirmation"
        LOST = "lost", "Lost"
        PRODUCTION = "production", "Production"
        DELIVERY = "delivery", "Delivery"
        INSTALLATION = "installation", "Installation"
        HANDOVER = "handover", "Handing Over"
        UNDER_WARRANTY = "under_warranty", "Under Warranty"
        EXTENDED_WARRANTY = "extended_warranty", "Extended Warranty"
        DECOMMISSIONED = "decommissioned", "De-Commissioned"

    name = models.CharField(max_length=300)
    phase = models.CharField(max_length=25, choices=Phase.choices, default=Phase.QUERY)
    description = models.TextField(blank=True)
    location = models.CharField(max_length=300, blank=True)
    image = models.ImageField(upload_to=upload_to_path, blank=True)
    client = models.ForeignKey(
        "clients.Client", on_delete=models.SET_NULL, null=True, blank=True, related_name="projects"
    )
    site = models.ForeignKey(
        "sites.Site", on_delete=models.SET_NULL, null=True, blank=True, related_name="projects"
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PLANNING)
    progress = models.PositiveSmallIntegerField(default=0, help_text="Percentage 0-100")
    start_date = models.DateField(null=True, blank=True)
    target_date = models.DateField(null=True, blank=True)
    completed_date = models.DateField(null=True, blank=True)
    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="managed_projects"
    )
    budget = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    notes = models.TextField(blank=True)
    source_work_order = models.ForeignKey(
        "workorders.WorkOrder", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="projects", help_text="Work order this project was created from",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class ProjectScopeItem(TimeStampedModel):
    """One line of a project's scope: an asset (and optionally one of its
    components) deployed in some quantity at some location, possibly with its
    own start date — one order can span many assets across many sites."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="scope_items")
    device = models.ForeignKey(
        "assets.Device", on_delete=models.CASCADE, related_name="project_scope_items"
    )
    component = models.ForeignKey(
        "assets.AssetComponent", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="project_scope_items",
    )
    quantity = models.PositiveIntegerField(default=1)
    site = models.ForeignKey(
        "sites.Site", on_delete=models.SET_NULL, null=True, blank=True, related_name="project_scope_items"
    )
    start_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.project.name}: {self.device.asset_code} ×{self.quantity}"


class ProjectMilestone(TimeStampedModel):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="milestones")
    title = models.CharField(max_length=300)
    due_date = models.DateField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "due_date", "created_at"]

    def __str__(self):
        return f"{self.project.name} — {self.title}"


class ProjectBottleneck(TimeStampedModel):
    class Severity(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"
        CRITICAL = "critical", "Critical"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="bottlenecks")
    title = models.CharField(max_length=300)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.MEDIUM)
    is_resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.project.name} - {self.title}"


class ProjectMember(TimeStampedModel):
    class Role(models.TextChoices):
        LEAD = "lead", "Team Lead"
        MEMBER = "member", "Member"
        OBSERVER = "observer", "Observer"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="project_memberships"
    )
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)

    class Meta:
        unique_together = ["project", "user"]

    def __str__(self):
        return f"{self.user.get_full_name()} on {self.project.name}"
