from django.conf import settings
from django.db import models

from common.models import TimeStampedModel


class MaintenanceSchedule(TimeStampedModel):
    class MaintenanceType(models.TextChoices):
        """Work is either planned ahead of a fault or a response to one.

        Preventive is scheduled — the rounds somebody sets up in advance.
        Corrective is raised when an asset goes down, from a ticket or from
        the asset itself. There is no third kind.
        """

        PREVENTIVE = "preventive", "Preventive"
        CORRECTIVE = "corrective", "Corrective"

    class Frequency(models.TextChoices):
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"
        MONTHLY = "monthly", "Monthly"
        QUARTERLY = "quarterly", "Quarterly"
        YEARLY = "yearly", "Yearly"
        ONE_TIME = "one_time", "One-Time"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PENDING = "pending", "Pending"
        IN_PROCESS = "in_process", "In Process"
        ON_HOLD = "on_hold", "On Hold"
        OVERDUE = "overdue", "Over Due"
        COMPLETED = "completed", "Completed"

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    title = models.CharField(max_length=300)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    maintenance_type = models.CharField(max_length=15, choices=MaintenanceType.choices, default=MaintenanceType.PREVENTIVE)
    frequency = models.CharField(max_length=15, choices=Frequency.choices, default=Frequency.MONTHLY)
    device = models.ForeignKey(
        "assets.Device", on_delete=models.CASCADE, null=True, blank=True, related_name="maintenance_schedules"
    )
    site = models.ForeignKey(
        "sites.Site", on_delete=models.SET_NULL, null=True, blank=True, related_name="maintenance_schedules"
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="maintenance_assignments"
    )
    # External vendors involved in this maintenance (can be several).
    vendors = models.ManyToManyField(
        "suppliers.Supplier", blank=True, related_name="maintenance_schedules"
    )
    # The day the arrangement comes into effect. next_due moves on with every
    # round completed; this stays put, so a schedule can still say when it
    # began after a year of visits.
    start_date = models.DateField(null=True, blank=True)
    next_due = models.DateField()
    instructions = models.TextField(blank=True)
    # What this maintenance needs on-site, entered freely at scheduling time:
    # a list of {"name": str, "quantity": int} rows (not tied to the asset's
    # registered components).
    required_components = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.ACTIVE)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["next_due"]

    def __str__(self):
        return f"{self.title} ({self.frequency})"

    def due_after(self, start):
        """When the round after ``start`` falls, given this frequency.

        A one-time job has no round after: it happens once, on the day it was
        arranged for.
        """
        from dateutil.relativedelta import relativedelta

        cycles = {
            self.Frequency.DAILY: relativedelta(days=1),
            self.Frequency.WEEKLY: relativedelta(weeks=1),
            self.Frequency.MONTHLY: relativedelta(months=1),
            self.Frequency.QUARTERLY: relativedelta(months=3),
            self.Frequency.YEARLY: relativedelta(years=1),
        }
        step = cycles.get(self.frequency)
        return start + step if step else start

    @classmethod
    def from_db(cls, db, field_names, values):
        row = super().from_db(db, field_names, values)
        row._loaded_start_date = row.start_date
        return row

    def save(self, *args, **kwargs):
        # The start date and the frequency say when the first visit falls, so
        # it is worked out rather than asked for. After that the date moves on
        # its own — a round completed, or one moved to a day that suits the
        # site — and recomputing it from the start would undo that.
        start_moved = (
            self.start_date is not None
            and self.start_date != getattr(self, "_loaded_start_date", None)
        )
        if self.start_date and (self._state.adding or start_moved or not self.next_due):
            self.next_due = self.due_after(self.start_date)
        elif not self.start_date and self.next_due:
            self.start_date = self.next_due
        # Maintenance happens where the asset stands. The site is recorded on
        # the asset when it is installed, so asking for it again here would
        # only create a second answer that could disagree with the first.
        # An asset with nowhere recorded overrides nothing: erasing a site
        # somebody set would be worse than the disagreement this avoids.
        if self.device_id is not None and self.device.current_site_id:
            self.site_id = self.device.current_site_id
        super().save(*args, **kwargs)
        self._loaded_start_date = self.start_date

    def advance_after_completion(self, performed_date):
        """Roll the schedule to its next cycle once a completed record lands."""
        if self.frequency == self.Frequency.ONE_TIME:
            self.status = self.Status.COMPLETED
            self.is_active = False
            self.save(update_fields=["status", "is_active", "updated_at"])
            return
        base = max(self.next_due, performed_date) if self.next_due else performed_date
        self.next_due = self.due_after(base)
        self.status = self.Status.ACTIVE
        self.save(update_fields=["next_due", "status", "updated_at"])

    def open_visit(self):
        """The round being planned, opened if there is not one yet.

        A live schedule always has exactly one: the next date it falls due,
        with whoever is going. Completing a round rolls the schedule and opens
        the round after it.
        """
        visit = (
            self.visits.filter(
                status__in=(MaintenanceVisit.Status.PLANNED, MaintenanceVisit.Status.IN_PROGRESS)
            )
            .order_by("due_date", "created_at")
            .first()
        )
        if visit is None:
            return MaintenanceVisit.objects.create(
                schedule=self, due_date=self.next_due, assigned_to=self.assigned_to,
            )
        # The open round is the next one due, so it follows the schedule when
        # the schedule is what moved.
        if visit.due_date != self.next_due:
            visit.due_date = self.next_due
            visit.save(update_fields=["due_date", "updated_at"])
        return visit

    @property
    def effective_status(self):
        """Auto-flag overdue schedules that aren't completed or on hold."""
        from django.utils import timezone

        if self.status in (self.Status.COMPLETED, self.Status.ON_HOLD):
            return self.status
        if self.next_due and self.next_due < timezone.now().date():
            return self.Status.OVERDUE
        return self.status


class MaintenanceVisit(TimeStampedModel):
    """One round of a schedule: the visit itself, planned or done.

    A preventive schedule is an arrangement, not a job — it comes round every
    month and whoever is free attends. So each round is its own row: when it
    is due, who is going, what it needs from the store, and what came back.
    The open round is always there to be planned against; completing it writes
    the record and opens the next.
    """

    class Status(models.TextChoices):
        PLANNED = "planned", "Planned"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETED = "completed", "Completed"
        SKIPPED = "skipped", "Skipped"

    schedule = models.ForeignKey(
        MaintenanceSchedule, on_delete=models.CASCADE, related_name="visits"
    )
    due_date = models.DateField()
    # Who attends this round. A schedule names a default; every round can go
    # to somebody else, which is the whole point of planning one at a time.
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="maintenance_visits",
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PLANNED)
    started_at = models.DateTimeField(null=True, blank=True)
    # What was recorded when the round was closed out.
    record = models.OneToOneField(
        "maintenance.MaintenanceRecord", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="visit",
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["due_date", "created_at"]

    def __str__(self):
        return f"{self.schedule.title} — {self.due_date}"

    @property
    def is_open(self) -> bool:
        return self.status in (self.Status.PLANNED, self.Status.IN_PROGRESS)


class MaintenanceRecord(TimeStampedModel):
    class Status(models.TextChoices):
        COMPLETED = "completed", "Completed"
        SKIPPED = "skipped", "Skipped"
        PARTIAL = "partial", "Partial"

    schedule = models.ForeignKey(
        MaintenanceSchedule, on_delete=models.CASCADE, related_name="records"
    )
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="maintenance_records"
    )
    performed_at = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.COMPLETED)
    notes = models.TextField(blank=True)
    cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    # Cost liability (MW-01/02): derived from the asset's warranty state when
    # the record is created — expired warranty defaults to billable-to-client.
    is_billable = models.BooleanField(default=False)
    charge_to = models.CharField(
        max_length=10,
        choices=[("company", "Company"), ("client", "Client"), ("vendor", "Vendor")],
        blank=True,
        default="",
    )
    # Which of the asset's components were serviced/replaced during the visit.
    components_used = models.ManyToManyField(
        "assets.AssetComponent", blank=True, related_name="maintenance_records"
    )

    class Meta:
        ordering = ["-performed_at"]

    def __str__(self):
        return f"{self.schedule.title} - {self.performed_at.date()}"


class MaintenanceRecordPhoto(TimeStampedModel):
    record = models.ForeignKey(MaintenanceRecord, on_delete=models.CASCADE, related_name="photos")
    image = models.ImageField(upload_to="maintenance/photos/")
    caption = models.CharField(max_length=300, blank=True)
    taken_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True
    )

    def __str__(self):
        return f"Photo for {self.record}"


class MaintenancePartRequest(TimeStampedModel):
    """One part a technician has asked for, and what was agreed.

    Separate from the store's own queue: this is the asking and the answering,
    and only once a line is approved does it become something the store is
    expected to hand over.
    """

    class Status(models.TextChoices):
        REQUESTED = "requested", "Awaiting Approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Withdrawn"

    schedule = models.ForeignKey(
        MaintenanceSchedule, on_delete=models.CASCADE, related_name="part_requests"
    )
    # Generic stock or an opened unique product — exactly one, the way every
    # other requirement in the system names what it needs.
    item = models.ForeignKey(
        "inventory.InventoryItem", on_delete=models.PROTECT, null=True, blank=True,
        related_name="maintenance_part_requests",
    )
    unit_type = models.ForeignKey(
        "inventory.InventoryUnitType", on_delete=models.PROTECT, null=True, blank=True,
        related_name="maintenance_part_requests",
    )
    # What the technician called it, kept for lines typed before a stock row
    # existed and so a rejected line still reads sensibly.
    name = models.CharField(max_length=200, blank=True)

    quantity_requested = models.PositiveIntegerField()
    # Null until decided. Approving for less than was asked is the common
    # answer, so it is a quantity rather than a yes.
    quantity_approved = models.PositiveIntegerField(null=True, blank=True)

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.REQUESTED)
    reason = models.CharField(max_length=300, blank=True)

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="maintenance_parts_requested",
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="maintenance_parts_decided",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=300, blank=True)

    # The round this line belongs to. A schedule runs every month and asks for
    # parts every time, so without it a job's parts are one long list with no
    # way to tell which visit each was for.
    visit = models.ForeignKey(
        "maintenance.MaintenanceVisit", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="part_requests",
    )
    # What the visit did with what it was given. The two add up to what the
    # store issued: anything not used goes back, and is only back in stock
    # once receiving has inspected it.
    quantity_used = models.PositiveIntegerField(null=True, blank=True)
    quantity_returned = models.PositiveIntegerField(default=0)
    # The receipt the store inspects the returned material against.
    return_reference = models.CharField(max_length=50, blank=True)

    # The store request this line became once it was approved. Nothing here
    # moves stock; the store still issues it.
    issuance_request = models.OneToOneField(
        "inventory.IssuanceRequest", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="maintenance_part_request",
    )

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.what} ×{self.quantity_requested} for {self.schedule.title}"

    @property
    def what(self):
        """What was asked for, by the name the store would recognise."""
        if self.item_id:
            return self.item.material_type.name if self.item.material_type_id else self.item.sku
        if self.unit_type_id:
            return str(self.unit_type)
        return self.name or "—"

    @property
    def unit(self):
        """How this part is counted, from whichever stock row it names."""
        if self.item_id:
            mt = self.item.material_type
            return (mt.unit if mt is not None else "") or "piece"
        if self.unit_type_id:
            return self.unit_type.unit or "piece"
        return "piece"
