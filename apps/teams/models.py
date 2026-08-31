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

    class ContractType(models.TextChoices):
        SOLD = "sold", "Sold Outright"
        RENTAL = "rental", "Rental"

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
    contract_type = models.CharField(
        max_length=10, choices=ContractType.choices, blank=True, default="",
        help_text="Whether the project is sold outright or rented",
    )
    rental_end_date = models.DateField(null=True, blank=True)
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
    source_quotation = models.ForeignKey(
        "quotations.Quotation", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="spawned_projects", help_text="Quotation this project was created from",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    # The commercial ladder in delivery order (off-ramps excluded).
    MAIN_PHASE_ORDER = (
        "query", "quotation", "negotiation", "order_confirmation", "production",
        "delivery", "installation", "handover", "under_warranty",
        "extended_warranty", "decommissioned",
    )

    def computed_progress(self):
        """Progress is derived, never hand-typed: % of completed milestones
        when milestones exist, otherwise position along the phase ladder."""
        milestones = list(self.milestones.all())
        if milestones:
            done = sum(1 for m in milestones if m.completed_at)
            return round(done / len(milestones) * 100)
        if self.status == self.Status.COMPLETED:
            return 100
        if self.phase in self.MAIN_PHASE_ORDER:
            idx = self.MAIN_PHASE_ORDER.index(self.phase)
            return round(idx / (len(self.MAIN_PHASE_ORDER) - 1) * 100)
        # Off-ramp phases (on hold / lost): keep whatever was stored.
        return self.progress or 0


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


class ProjectBOMLine(TimeStampedModel):
    """One bill-of-materials line on a project: what needs to be provided
    (typed via asset_type / device_model / material_type), in what quantity,
    at what price. Fulfilment is tracked through BOMAllocation rows."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="bom_lines")
    asset_type = models.ForeignKey(
        "assets.AssetType", on_delete=models.SET_NULL, null=True, blank=True, related_name="bom_lines"
    )
    device_model = models.ForeignKey(
        "assets.DeviceModel", on_delete=models.SET_NULL, null=True, blank=True, related_name="bom_lines"
    )
    material_type = models.ForeignKey(
        "assets.MaterialType", on_delete=models.SET_NULL, null=True, blank=True, related_name="bom_lines"
    )
    description = models.CharField(max_length=300)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    source_quotation_item = models.ForeignKey(
        "quotations.QuotationItem", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="bom_lines", help_text="Quotation line this BOM line was copied from",
    )

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.project.name}: {self.description} x{self.quantity}"

    # Computed fulfilment figures. Cancelled allocations never count; issued
    # allocations stay "allocated" (the need is covered) and also count as
    # issued. Iterate in python so a prefetch_related("allocations") queryset
    # answers all three without extra queries.
    @property
    def allocated_quantity(self) -> int:
        return sum(
            a.quantity for a in self.allocations.all()
            if a.status != BOMAllocation.Status.CANCELLED
        )

    @property
    def issued_quantity(self) -> int:
        return sum(
            a.quantity for a in self.allocations.all()
            if a.status == BOMAllocation.Status.ISSUED
        )

    @property
    def shortage(self) -> int:
        return max(0, self.quantity - self.allocated_quantity)


class BOMAllocation(TimeStampedModel):
    """Reserves a specific device (unique item) or a slice of warehouse stock
    (generic item) against a BOM line. Stock allocations later flip to
    ``issued`` via the issue endpoint; devices are issued through
    installation, never here."""

    class Status(models.TextChoices):
        ALLOCATED = "allocated", "Allocated"
        ISSUED = "issued", "Issued"
        CANCELLED = "cancelled", "Cancelled"

    bom_line = models.ForeignKey(ProjectBOMLine, on_delete=models.CASCADE, related_name="allocations")
    device = models.ForeignKey(
        "assets.Device", on_delete=models.SET_NULL, null=True, blank=True, related_name="bom_allocations"
    )
    inventory_item = models.ForeignKey(
        "inventory.InventoryItem", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="bom_allocations",
    )
    quantity = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ALLOCATED)
    allocated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="bom_allocations",
    )

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        target = self.device or self.inventory_item
        return f"{self.bom_line}: {target} x{self.quantity} ({self.status})"


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
