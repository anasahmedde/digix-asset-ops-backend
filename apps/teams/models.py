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
        """How far a client order has got, in the steps the work actually takes.

        The commercial run-up is one phase to the delivery team; after it each
        phase is a body of work that can be measured against what it needs to
        finish.
        """

        PLANNING = "planning", "Planning"
        PROCUREMENT = "procurement", "Procurement"
        PRODUCTION = "production", "Production"
        INSTALLATION = "installation", "Installation"
        HANDOVER = "handover", "Handing Over"
        ON_HOLD = "on_hold", "On Hold"
        LOST = "lost", "Order Lost"

    name = models.CharField(max_length=300)
    phase = models.CharField(max_length=25, choices=Phase.choices, default=Phase.PLANNING)
    description = models.TextField(blank=True)
    location = models.CharField(max_length=300, blank=True)
    image = models.ImageField(upload_to=upload_to_path, blank=True)
    client = models.ForeignKey(
        "clients.Client", on_delete=models.SET_NULL, null=True, blank=True, related_name="projects"
    )
    site = models.ForeignKey(
        "sites.Site", on_delete=models.SET_NULL, null=True, blank=True, related_name="projects"
    )
    # One order can put assets up at several sites. `site` stays as the
    # primary for older screens; this is the full list.
    sites = models.ManyToManyField("sites.Site", blank=True, related_name="scoped_projects")
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
        Phase.PLANNING, Phase.PROCUREMENT, Phase.PRODUCTION,
        Phase.INSTALLATION, Phase.HANDOVER,
    )

    def phase_from_work(self):
        """The first phase that is not finished — that is where the project is.

        A phase is done when its bar reads 100%. When every one of them does,
        the project sits on the last phase with nothing left in it. On Hold and
        Order Lost are off-ramps somebody chooses, so the work does not
        overrule them.
        """
        if self.phase in (self.Phase.ON_HOLD, self.Phase.LOST):
            return self.phase
        from .phases import phase_progress

        bars = phase_progress(self)
        for phase in self.MAIN_PHASE_ORDER:
            if bars[phase]["percent"] < 100:
                return phase
        return self.MAIN_PHASE_ORDER[-1]

    def sync_phase(self):
        """Put the stored phase back in step with the work, and say what it is.

        The phase is stored rather than worked out on demand because lists are
        filtered by it. Storing it means it can fall behind, so reading it is
        also when it gets corrected — which costs a write only on the read
        where the work has actually moved on.
        """
        settled = self.phase_from_work()
        changed = []
        if settled != self.phase:
            self.phase = settled
            changed.append("phase")
        # Everything finished is the one status the work can declare on its
        # own; the rest are judgements somebody makes about how it is going.
        if (
            settled == self.MAIN_PHASE_ORDER[-1]
            and self.status != self.Status.COMPLETED
            and self.computed_progress() >= 100
        ):
            self.status = self.Status.COMPLETED
            changed.append("status")
        if changed:
            self.save(update_fields=[*changed, "updated_at"])
        return settled

    def computed_progress(self):
        """How far the order has got, derived rather than hand-typed.

        Milestones win where a team keeps them. Otherwise it is the phases,
        each worth an equal share of the project and each filled by its own
        work. It deliberately does not consult the stored phase: that is a
        label somebody sets by hand, and a project whose parts are all in and
        whose assets are all built has made that progress whether or not
        anybody remembered to move the marker.
        """
        milestones = list(self.milestones.all())
        if milestones:
            done = sum(1 for m in milestones if m.completed_at)
            return round(done / len(milestones) * 100)
        if self.status == self.Status.COMPLETED:
            return 100
        if self.phase not in self.MAIN_PHASE_ORDER:
            # Off-ramp phases (on hold / lost): keep whatever was stored.
            return self.progress or 0
        from .phases import phase_progress

        bars = phase_progress(self)
        return min(100, round(
            sum(bars[phase]["percent"] for phase in self.MAIN_PHASE_ORDER)
            / len(self.MAIN_PHASE_ORDER)
        ))


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
        constraints = [
            # Every asset has its own ID; it is on a project once, not counted.
            models.UniqueConstraint(fields=["project", "device"], name="uniq_asset_per_project"),
        ]

    def __str__(self):
        return f"{self.project.name}: {self.device.asset_code}"


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
    # Goods now reach the warehouse as inventory (via receipt inspection)
    # rather than as Devices, so a serialized allocation targets the unit.
    inventory_unit = models.ForeignKey(
        "inventory.InventoryUnit", on_delete=models.SET_NULL, null=True, blank=True,
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
        target = self.device or self.inventory_unit or self.inventory_item
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


class ProjectBudget(TimeStampedModel):
    """The cost plan for a project, and whether it has been signed off.

    Planning comes before execution: the estimate is built from what the
    assets need (priced from inventory and past purchases), plus overheads and
    a contingency, and the total goes up for approval. Only an approved budget
    lets the project start drawing stock or raising purchases.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Awaiting Approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    project = models.OneToOneField(Project, on_delete=models.CASCADE, related_name="cost_plan")
    contingency_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text="Added on top of materials and overheads, as a percentage",
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="budgets_submitted",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="budgets_decided",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_notes = models.TextField(blank=True)
    # What was signed off, frozen at approval — the estimate keeps moving as
    # prices change, the approved figure must not.
    approved_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    def __str__(self):
        return f"Budget for {self.project.name} ({self.get_status_display()})"

    @property
    def is_editable(self):
        return self.status in (self.Status.DRAFT, self.Status.REJECTED)


class ProjectCostLine(TimeStampedModel):
    """An overhead on the project plan: travel, labour, transport, and so on.

    The type is the user's own word for it rather than a fixed list — every
    company slices overheads differently.
    """

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="cost_lines")
    cost_type = models.CharField(max_length=100, blank=True)
    description = models.CharField(max_length=300, blank=True)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=1)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2)
    # What it actually came to. The planned figures stay as they were
    # approved; these move with reality during execution.
    actual_quantity = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    actual_unit_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.cost_type}: {self.description or ''} ({self.amount})"

    @property
    def amount(self):
        return (self.quantity or 0) * (self.unit_cost or 0)

    @property
    def actual_amount(self):
        """None until someone records what it cost - not the same as zero."""
        if self.actual_unit_cost is None:
            return None
        quantity = self.actual_quantity if self.actual_quantity is not None else 1
        return quantity * self.actual_unit_cost
