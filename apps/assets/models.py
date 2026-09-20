from django.conf import settings
from django.db import models

from common.codes import generate_code
from common.models import TimeStampedModel
from common.utils import upload_to_path


class Brand(TimeStampedModel):
    name = models.CharField(max_length=200, unique=True)
    website = models.URLField(blank=True)
    logo = models.ImageField(upload_to="brands/", blank=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class DeviceModel(TimeStampedModel):
    """Hardware model/SKU within a brand."""

    brand = models.ForeignKey(Brand, on_delete=models.CASCADE, related_name="device_models")
    name = models.CharField(max_length=200)
    model_number = models.CharField(max_length=100, blank=True)
    screen_type = models.CharField(max_length=100, blank=True)
    screen_size = models.CharField(max_length=50, blank=True)
    specifications = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ["brand", "name"]

    def __str__(self):
        return f"{self.brand.name} {self.name}"


class MaterialType(TimeStampedModel):
    """A component: something an asset is built from (cables, mounts, players…).

    Shown to users as "Component". Its category is one of the inventory
    categories — the old free-text category duplicated that list, which is
    exactly what the client flagged.
    """

    name = models.CharField(max_length=200, unique=True)
    legacy_category = models.CharField(max_length=100, blank=True, editable=False)
    category = models.ForeignKey(
        "inventory.InventoryCategory", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="components",
    )
    # Unit of measure: piece, meter, box, kg …
    unit = models.CharField(max_length=50, default="piece")
    description = models.TextField(blank=True)

    class Meta:
        verbose_name = "component"
        ordering = ["name"]

    def __str__(self):
        return self.name


class AssetType(TimeStampedModel):
    """
    Category of asset shown in the registry and details
    (SMD Screen, Standee, Talker, Digital Display, Tokenomo, …).

    Data-driven so new types can be added from the Setup screens without a
    code change.
    """

    name = models.CharField(max_length=150, unique=True)
    code = models.CharField(max_length=30, blank=True, db_index=True)
    description = models.TextField(blank=True)
    has_dimensions = models.BooleanField(
        default=False, help_text="Uses length × width (e.g. SMD screens)"
    )
    has_diagonal = models.BooleanField(
        default=False, help_text="Uses diagonal size in inches (e.g. digital displays)"
    )
    icon = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Device(TimeStampedModel):
    class Status(models.TextChoices):
        PROCURED = "procured", "In Procurement"
        IN_PRODUCTION = "in_production", "In Production"
        IN_STOCK = "in_stock", "In Stock"
        ASSIGNED = "assigned", "Assigned to Client"
        INSTALLED = "installed", "Installed"
        ACTIVE = "active", "Active"
        UNDER_MAINTENANCE = "under_maintenance", "Under Maintenance"
        CLIENT_PROPERTY = "client_property", "Client Property"
        DECOMMISSIONED = "decommissioned", "Decommissioned"
        LOST_STOLEN = "lost_stolen", "Lost/Stolen"
        RMA = "rma", "RMA"
        IN_TRANSIT = "in_transit", "In Transit"

    class Source(models.TextChoices):
        """How the asset comes into existence and who installs it.

        The route decides what the asset needs: an in-house build carries
        components and production steps, while a vendor-built one does not.
        """

        # Built by us from inventory components, following production steps.
        INHOUSE = "inhouse", "In-house Production"
        # Bought complete from a vendor, but installed by our own technician.
        VENDOR_SUPPLIED = "vendor_supplied", "Vendor Supplied · Installed In-house"
        # Vendor builds and installs it; our technician oversees the work.
        VENDOR_TURNKEY = "vendor_turnkey", "Vendor Supplied & Installed"

    # Enforced status machine — status changes go through the /transition/
    # action (see views.DeviceViewSet.transition); every flip is journalled
    # as a DeviceLifecycleEvent + AuditLog entry by signals.py.
    VALID_TRANSITIONS = {
        # Assets are built here from inventory components, so a procured asset
        # goes to the production floor or straight to stock — never "in transit"
        # (that is a movement state for assets that already exist).
        Status.PROCURED: (Status.IN_PRODUCTION, Status.IN_STOCK, Status.RMA),
        Status.IN_PRODUCTION: (Status.IN_STOCK, Status.RMA),
        Status.IN_TRANSIT: (Status.IN_STOCK, Status.IN_PRODUCTION),
        Status.IN_STOCK: (
            Status.ASSIGNED, Status.IN_TRANSIT,
            Status.DECOMMISSIONED, Status.LOST_STOLEN,
        ),
        Status.ASSIGNED: (Status.INSTALLED, Status.IN_STOCK, Status.IN_TRANSIT),
        Status.INSTALLED: (Status.ACTIVE, Status.UNDER_MAINTENANCE, Status.RMA),
        Status.ACTIVE: (
            Status.UNDER_MAINTENANCE, Status.RMA, Status.CLIENT_PROPERTY,
            Status.IN_TRANSIT, Status.DECOMMISSIONED, Status.LOST_STOLEN,
        ),
        Status.UNDER_MAINTENANCE: (Status.ACTIVE, Status.RMA, Status.DECOMMISSIONED),
        Status.RMA: (Status.IN_STOCK, Status.DECOMMISSIONED),
        Status.CLIENT_PROPERTY: (Status.DECOMMISSIONED,),
        Status.LOST_STOLEN: (Status.IN_STOCK,),
        Status.DECOMMISSIONED: (),
    }

    asset_code = models.CharField(max_length=50, unique=True, db_index=True)
    # Not entered by hand: every asset gets a generated asset_code with a
    # QR/barcode label, and the serial defaults to it. Kept as its own
    # field so a manufacturer serial can still be recorded when there is one.
    serial_number = models.CharField(max_length=200, unique=True, blank=True)
    mobile_id = models.CharField(max_length=200, blank=True, help_text="Linked CMS device ID")
    mac_address = models.CharField(max_length=17, blank=True)
    imei = models.CharField(max_length=20, blank=True)

    asset_type = models.ForeignKey(
        AssetType, on_delete=models.PROTECT, null=True, blank=True, related_name="devices",
        help_text="Asset category (SMD Screen, Standee, Digital Display, …)",
    )
    # Deprecated on the asset itself: identity now comes from asset_type +
    # display_name + its components. Kept (nullable) so historical assets
    # keep their reference; DeviceModel is still used by BOM, quotation
    # and purchase-order lines.
    device_model = models.ForeignKey(
        DeviceModel, on_delete=models.PROTECT, null=True, blank=True, related_name="devices"
    )
    display_name = models.CharField(max_length=200, blank=True, help_text="Friendly asset name")
    firmware_version = models.CharField(max_length=100, blank=True)
    hardware_revision = models.CharField(max_length=100, blank=True)

    # Physical size in inches: length × width × depth (e.g. SMD screens)
    # and/or diagonal (displays).
    length_in = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    width_in = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    depth_in = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    diagonal_inches = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PROCURED)
    # Registration origin: built in-house or bought from a third party (WF-05).
    source = models.CharField(
        max_length=20, choices=Source.choices, default=Source.INHOUSE, db_index=True
    )

    image = models.ImageField(upload_to=upload_to_path, blank=True, help_text="Primary device photo")

    purchase_date = models.DateField(null=True, blank=True)
    purchase_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    # For an asset bought complete from a vendor: the purchase-order line that
    # buys it. Receiving that line is what brings the asset into stock.
    procurement_item = models.ForeignKey(
        "procurement.PurchaseOrderItem", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="procured_devices",
    )
    # A vendor-supplied asset on a project is sent to Procurement from the
    # project's Execution tab once the budget is approved; standalone assets
    # go straight to the to-buy list.
    procurement_requested_at = models.DateTimeField(null=True, blank=True)
    supplier = models.ForeignKey(
        "suppliers.Supplier", on_delete=models.SET_NULL, null=True, blank=True, related_name="devices"
    )
    invoice_reference = models.CharField(max_length=200, blank=True)
    batch_number = models.CharField(max_length=100, blank=True)

    current_site = models.ForeignKey(
        "sites.Site", on_delete=models.SET_NULL, null=True, blank=True, related_name="devices"
    )
    # Client hierarchy: Project (client order) -> Assets (this record) ->
    # Components. One order can span many assets across locations.
    project = models.ForeignKey(
        "teams.Project", on_delete=models.SET_NULL, null=True, blank=True, related_name="devices"
    )
    assigned_client = models.ForeignKey(
        "clients.Client", on_delete=models.SET_NULL, null=True, blank=True, related_name="devices"
    )
    # An asset can serve more than one client (e.g. a shared screen). The
    # primary owner stays assigned_client; these are the additional ones.
    clients = models.ManyToManyField(
        "clients.Client", blank=True, related_name="shared_devices"
    )
    # Two different vendors can be involved and they are not the same thing:
    # one sells us the finished asset, the other puts it up. On a turnkey job
    # they are usually the same firm, but nothing says they have to be — and on
    # a vendor-supplied asset our own technician installs it, so there is a
    # supplying vendor and no installing one.
    supply_vendor_name = models.CharField(
        max_length=200, blank=True, help_text="Vendor the finished asset was bought from",
    )
    supply_vendor_contact = models.CharField(
        max_length=100, blank=True, help_text="Phone or contact person for the supplying vendor",
    )
    # Who the asset is assigned to for installation. Captured when the status
    # moves to `assigned`: an internal technician (from the manpower records)
    # and/or an external vendor entered by hand.
    assigned_technician = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_devices"
    )
    # Picked from the vendors on the register. The name is kept alongside so a
    # vendor recorded before the register existed still reads correctly, and so
    # removing a vendor does not erase who did the work.
    assigned_vendor = models.ForeignKey(
        "suppliers.Supplier", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="installing_devices", help_text="Vendor installing this asset",
    )
    assigned_vendor_name = models.CharField(
        max_length=200, blank=True, help_text="External vendor installing this asset",
    )
    assigned_vendor_contact = models.CharField(
        max_length=100, blank=True, help_text="Phone or contact person for the installing vendor",
    )
    installation_date = models.DateField(null=True, blank=True)
    installed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="installed_devices", help_text="Who installed this asset",
    )

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["serial_number"]),
            models.Index(fields=["asset_code"]),
        ]

    def __str__(self):
        label = self.display_name or (
            self.asset_type.name if self.asset_type_id
            else (str(self.device_model) if self.device_model_id else "asset")
        )
        return f"{self.asset_code} ({label})"

    def save(self, *args, **kwargs):
        if not self.asset_code:
            self.asset_code = generate_code("asset", model=type(self), field="asset_code")
        # Fall back to the generated code so the unique constraint never sees
        # two blanks.
        if not self.serial_number:
            self.serial_number = self.asset_code
        super().save(*args, **kwargs)

    def can_transition_to(self, new_status: str) -> bool:
        allowed = self.VALID_TRANSITIONS.get(self.status, ())
        # A vendor-supplied asset arrives complete: it is never built here.
        if new_status == self.Status.IN_PRODUCTION and self.source != self.Source.INHOUSE:
            return False
        return new_status in allowed

    @property
    def is_locked(self) -> bool:
        """Once the project is executing, the build definition is fixed.

        The budget that was approved priced *this* parts list and *this* route;
        changing either afterwards would make the approval meaningless. Status
        still moves — the build progresses — but what it is built from does not.
        """
        plan = getattr(self.project, "cost_plan", None) if self.project_id else None
        if plan is not None and plan.status == "approved":
            return True
        # An asset in a project's scope is bound by that project's budget too.
        from apps.teams.models import ProjectScopeItem

        if ProjectScopeItem.objects.filter(device=self, project__cost_plan__status="approved").exists():
            return True
        return self.components.filter(
            models.Q(issued_quantity__gt=0) | models.Q(purchase_order_item__isnull=False)
        ).exists()

    @property
    def route_complete(self) -> bool:
        """Every operation on the route is done (or skipped) — the build is finished."""
        steps = list(self.production_steps.all())
        return bool(steps) and all(s.status in ("completed", "skipped") for s in steps)


class ProductionRouteTemplate(TimeStampedModel):
    """The standard build route for an asset type.

    The first time an asset type is produced someone works out the sequence of
    operations. Saving it here means the next asset of that type starts from
    the known route instead of being reinvented — the classic ERP routing
    master. One template per asset type; steps live on it.
    """

    asset_type = models.OneToOneField(
        AssetType, on_delete=models.CASCADE, related_name="route_template"
    )
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="route_templates",
    )

    class Meta:
        ordering = ["asset_type__name"]

    def __str__(self):
        return f"Route for {self.asset_type.name}"


class ProductionRouteTemplateStep(TimeStampedModel):
    """One operation in a saved route, copied onto each new asset of that type."""

    template = models.ForeignKey(
        ProductionRouteTemplate, on_delete=models.CASCADE, related_name="steps"
    )
    step_number = models.PositiveSmallIntegerField()
    name = models.CharField(max_length=200)
    location = models.CharField(max_length=12, default="in_house")
    workshop = models.ForeignKey(
        "suppliers.Supplier", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="route_template_steps",
    )
    workshop_name = models.CharField(max_length=200, blank=True)
    expected_days = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["step_number"]
        unique_together = ["template", "step_number"]

    def __str__(self):
        return f"{self.template.asset_type.name} · {self.step_number}. {self.name}"


class ComponentTemplate(TimeStampedModel):
    """The standard bill of materials for an asset type.

    The sibling of ProductionRouteTemplate: that one says how a type is built,
    this one says what it is built from. Working the parts list out once means
    the next asset of the same type starts from it instead of being itemised
    again by hand. One template per asset type; lines live on it.
    """

    asset_type = models.OneToOneField(
        AssetType, on_delete=models.CASCADE, related_name="component_template"
    )
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="component_templates",
    )

    class Meta:
        ordering = ["asset_type__name"]

    def __str__(self):
        return f"Components for {self.asset_type.name}"


class ComponentTemplateLine(TimeStampedModel):
    """One part in a saved bill of materials, copied onto each new asset."""

    template = models.ForeignKey(
        ComponentTemplate, on_delete=models.CASCADE, related_name="lines"
    )
    # Mirrors AssetComponent: exactly one of the two points at what is needed.
    inventory_item = models.ForeignKey(
        "inventory.InventoryItem", on_delete=models.CASCADE, null=True, blank=True,
        related_name="component_template_lines",
    )
    inventory_unit_type = models.ForeignKey(
        "inventory.InventoryUnitType", on_delete=models.CASCADE, null=True, blank=True,
        related_name="component_template_lines",
    )
    quantity = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        source = self.inventory_unit_type or self.inventory_item
        return f"{self.template.asset_type.name} · {source} ×{self.quantity}"


class ProductionStep(TimeStampedModel):
    """One operation in the route that turns components into a finished asset.

    Only in-house builds have these. The initiator lays out the sequence up
    front — some operations happen on our own floor, others go out to a
    workshop (a standee leaving for painting or panaflex and coming back) —
    so the asset shows where it physically is at any point in the build.
    """

    class Location(models.TextChoices):
        # Where an operation happens is the project's call, made in Execution.
        UNDECIDED = "undecided", "Not decided"
        IN_HOUSE = "in_house", "In-house"
        EXTERNAL = "external", "Outside Workshop"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        IN_PROGRESS = "in_progress", "In Progress"
        SENT_OUT = "sent_out", "Work Order Raised"
        RETURNED = "returned", "Returned from Workshop"
        COMPLETED = "completed", "Completed"
        SKIPPED = "skipped", "Skipped"

    # An external operation is only meaningfully "sent"/"returned"; an in-house
    # one just runs. Both converge on completed.
    # The moves a person makes on an in-house operation. A step on a work
    # order is not moved by hand: Work Order Raised and Completed follow the
    # work order itself (see workorders.signals).
    VALID_TRANSITIONS = {
        Status.PENDING: (Status.IN_PROGRESS, Status.COMPLETED),
        Status.IN_PROGRESS: (Status.COMPLETED,),
        Status.SENT_OUT: (),
        Status.RETURNED: (Status.IN_PROGRESS, Status.COMPLETED),
        Status.COMPLETED: (),
        Status.SKIPPED: (),
    }

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="production_steps")
    step_number = models.PositiveSmallIntegerField()
    name = models.CharField(max_length=200, help_text="e.g. Frame welding, Panaflex pasting")
    location = models.CharField(max_length=12, choices=Location.choices, default=Location.UNDECIDED)
    # Where the work goes when it leaves the building.
    workshop = models.ForeignKey(
        "suppliers.Supplier", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="production_steps",
    )
    workshop_name = models.CharField(
        max_length=200, blank=True, help_text="Workshop named by hand when it is not a registered supplier"
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="production_steps",
    )
    expected_days = models.PositiveSmallIntegerField(null=True, blank=True)
    # What the planner expects this operation to cost. Unset until someone
    # prices it — an unpriced step is not a free step.
    planned_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    actual_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    returned_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    # When Execution asked for a work order. Cleared once one is raised, or
    # when the decision changes.
    work_order_requested_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["step_number", "created_at"]
        unique_together = ["device", "step_number"]
        indexes = [models.Index(fields=["device", "status"])]

    def __str__(self):
        return f"{self.device.asset_code} · {self.step_number}. {self.name}"

    def can_transition_to(self, new_status) -> bool:
        return new_status in self.VALID_TRANSITIONS.get(self.status, ())

    def live_work_orders(self):
        """Work orders covering this operation — named on the order itself
        (older single-step orders) or on one of its lines — not cancelled."""
        from apps.workorders.models import WorkOrder

        return (
            WorkOrder.objects.filter(
                models.Q(production_step=self) | models.Q(items__production_step=self)
            )
            .exclude(status=WorkOrder.Status.CANCELLED)
            .distinct()
            .order_by("created_at")
        )

    @property
    def live_work_order(self):
        return self.live_work_orders().last()

    @property
    def on_a_work_order(self) -> bool:
        """The operation is genuinely in a vendor's hands, or queued to be.

        An operation marked external with no live order and no request behind
        it is external in name only — nothing is going to move it, so it stays
        the floor's to run rather than waiting forever on an order that does
        not exist.
        """
        return self.work_order_requested or self.live_work_orders().exists()

    @property
    def work_order_requested(self) -> bool:
        """Execution asked for a work order that nobody has raised yet."""
        return (
            self.location == self.Location.EXTERNAL
            and self.work_order_requested_at is not None
            and not self.live_work_orders().exists()
        )

    @property
    def on_project(self) -> bool:
        device = self.device
        return bool(device.project_id) or device.project_scope_items.exists()

    @property
    def manual_moves(self) -> tuple:
        """What a person may move this step to right now."""
        if self.location == self.Location.EXTERNAL and self.on_a_work_order:
            return ()  # follows its work order
        if self.location == self.Location.UNDECIDED and self.on_project:
            return ()  # the project decides first
        return self.VALID_TRANSITIONS.get(self.status, ())

    @property
    def hold_reason(self) -> str:
        if self.status in (self.Status.COMPLETED, self.Status.SKIPPED):
            return ""
        if self.location == self.Location.EXTERNAL and self.on_a_work_order:
            if self.work_order_requested:
                return "Work order requested — raise it under Work Orders › Requests; the status then follows the order."
            return "On a work order — its status follows the work order."
        if self.location == self.Location.UNDECIDED and self.on_project:
            return "Decide in the project's Execution tab whether this is done in-house or on a work order."
        return ""

    @property
    def workshop_display(self):
        if self.location != self.Location.EXTERNAL:
            return None
        if self.workshop_id:
            return self.workshop.name
        if self.work_order_requested:
            return "Work order requested"
        return self.workshop_name or "Unnamed workshop"


class DeviceLifecycleEvent(TimeStampedModel):
    class EventType(models.TextChoices):
        STATUS_CHANGE = "status_change", "Status Change"
        REASSIGNMENT = "reassignment", "Reassignment"
        MAINTENANCE = "maintenance", "Maintenance"
        FIRMWARE_UPDATE = "firmware_update", "Firmware Update"
        LOCATION_CHANGE = "location_change", "Location Change"
        NOTE = "note", "Note"

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="lifecycle_events")
    event_type = models.CharField(max_length=20, choices=EventType.choices)
    from_value = models.CharField(max_length=200, blank=True)
    to_value = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="device_events"
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.device.asset_code} - {self.event_type}"


class AssetComponent(TimeStampedModel):
    """One piece of equipment inside a composed asset (the client's "Device").

    An SMD wall asset = cabinets + media player + power supplies; a standee is
    a single-component asset. Tickets/warranties stay at the asset level.
    """

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="components")
    name = models.CharField(max_length=200, help_text="e.g. SMD Cabinet P3.9")
    component_type = models.CharField(max_length=100, blank=True, help_text="e.g. Cabinet, Media Player, PSU")
    serial_number = models.CharField(max_length=200, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    supplier = models.ForeignKey(
        "suppliers.Supplier", on_delete=models.SET_NULL, null=True, blank=True, related_name="supplied_components"
    )
    # --- What this asset is built from -------------------------------------
    # A component is a *requirement*: which inventory item, and how many of it
    # this asset needs. It deliberately does NOT touch stock — you can specify
    # a build before the parts exist. The project decides later whether to
    # cover each requirement from stock or by procuring it, and that decision
    # is what moves the warehouse.
    #
    # Generic requirement -> inventory_item; unique requirement -> the opened
    # product (inventory_unit_type). Exactly one, enforced in the serializer.
    inventory_item = models.ForeignKey(
        "inventory.InventoryItem", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="asset_components",
    )
    inventory_unit_type = models.ForeignKey(
        "inventory.InventoryUnitType", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="asset_components",
    )
    # Set once the requirement is fulfilled by a specific serialized unit.
    inventory_unit = models.OneToOneField(
        "inventory.InventoryUnit", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="asset_component",
    )

    # --- How the requirement gets covered ----------------------------------
    # Decided in the Project section: draw it from existing stock, or procure
    # it. Procurement stays available even when stock is on hand — that call
    # belongs to the user, not the system.
    class Fulfilment(models.TextChoices):
        PENDING = "pending", "Not Decided"
        FROM_STOCK = "from_stock", "From Inventory"
        PROCUREMENT = "procurement", "To Be Procured"
        FULFILLED = "fulfilled", "Fulfilled"

    fulfilment = models.CharField(
        max_length=12, choices=Fulfilment.choices, default=Fulfilment.PENDING, db_index=True
    )
    issued_quantity = models.PositiveIntegerField(
        default=0, help_text="How much of the requirement has actually left the warehouse"
    )
    purchase_order_item = models.ForeignKey(
        "procurement.PurchaseOrderItem", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="asset_components", help_text="PO line raised to cover this requirement",
    )
    # Unit of measure carried from the component definition, kept here so a
    # requirement still reads correctly if the definition changes later.
    unit = models.CharField(max_length=50, blank=True)
    # A price the planner sets by hand, when they know something the record
    # does not — a fresh quote, a price rise. Overrides the derived price.
    planned_unit_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    # --- Raising the requirement mid-project -------------------------------
    # Wanting more than was planned costs money that was already signed off,
    # so it is asked for and granted, never simply taken.
    pending_increase = models.PositiveIntegerField(null=True, blank=True)
    increase_reason = models.CharField(max_length=20, blank=True)
    increase_notes = models.TextField(blank=True)
    increase_requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="component_increase_requests",
    )
    increase_requested_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            # One line per component per asset; more of it is a bigger quantity.
            models.UniqueConstraint(
                fields=["device", "inventory_item"],
                condition=models.Q(inventory_item__isnull=False),
                name="uniq_component_generic_per_device",
            ),
            models.UniqueConstraint(
                fields=["device", "inventory_unit_type"],
                condition=models.Q(inventory_unit_type__isnull=False),
                name="uniq_component_unique_per_device",
            ),
        ]

    def __str__(self):
        return f"{self.device.asset_code} · {self.name} ×{self.quantity}"

    @property
    def outstanding_quantity(self) -> int:
        """How much of this requirement is still to be covered."""
        return max(0, self.quantity - self.issued_quantity)

    def _open_requests(self):
        return [
            r for r in self.issuance_requests.all()
            if r.status not in ("cancelled", "fulfilled")
        ]

    @property
    def stock_requested_quantity(self) -> int:
        """How much the store has been asked to issue from inventory."""
        return sum(r.outstanding_quantity for r in self._open_requests() if not r.awaiting_procurement)

    @property
    def procure_quantity(self) -> int:
        """How much has been decided to buy (each Procure decision is a request)."""
        return sum(r.outstanding_quantity for r in self._open_requests() if r.awaiting_procurement)

    @property
    def undecided_quantity(self) -> int:
        """What is still to be covered and has no decision on it yet."""
        return max(0, self.outstanding_quantity - self.stock_requested_quantity - self.procure_quantity)

    @property
    def available_quantity(self) -> int:
        """On-hand stock for whatever this requirement points at."""
        if self.inventory_unit_type_id:
            return self.inventory_unit_type.in_stock_count
        if self.inventory_item_id:
            return self.inventory_item.quantity
        return 0


class DeviceImage(TimeStampedModel):
    """Gallery of images for a device."""

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to=upload_to_path)
    caption = models.CharField(max_length=300, blank=True)
    is_primary = models.BooleanField(default=False)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "-created_at"]

    def __str__(self):
        return f"{self.device.asset_code} - image {self.sort_order}"


class AssetCode(TimeStampedModel):
    """Generated QR/barcode labels for devices."""

    class LabelFormat(models.TextChoices):
        QR = "qr", "QR Code"
        BARCODE_128 = "code128", "Code 128 Barcode"

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="asset_labels")
    format = models.CharField(max_length=10, choices=LabelFormat.choices, default=LabelFormat.QR)
    label_size = models.CharField(max_length=20, default="60x30")
    generated_file = models.FileField(upload_to=upload_to_path, blank=True)
    is_current = models.BooleanField(default=True)
    printed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.device.asset_code} - {self.format}"
