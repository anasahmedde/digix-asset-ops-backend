from django.conf import settings
from django.db import models
from django.utils import timezone

from common.codes import generate_code
from common.models import TimeStampedModel


class InventoryCategory(TimeStampedModel):
    """Consumables / Spare / PPE / Safety items / Tools / Stock items, …"""

    name = models.CharField(max_length=150, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "inventory categories"
        ordering = ["name"]

    def __str__(self):
        return self.name


class InventoryItem(TimeStampedModel):
    class Location(models.TextChoices):
        # Inventory lives in the warehouse or in transit — never "on site".
        # Assets are what live on site; consumption is modelled via Issuance.
        WAREHOUSE = "warehouse", "Warehouse"
        IN_TRANSIT = "in_transit", "In Transit"

    material_type = models.ForeignKey(
        "assets.MaterialType", on_delete=models.PROTECT, related_name="inventory_items"
    )
    category = models.ForeignKey(
        InventoryCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name="items"
    )
    sku = models.CharField(max_length=100, unique=True, blank=True, db_index=True)
    quantity = models.IntegerField(default=0)
    min_stock_level = models.IntegerField(default=5)
    location = models.CharField(max_length=15, choices=Location.choices, default=Location.WAREHOUSE)
    # Where the stock is actually put: a rack, a bin, a room. Free text — every
    # store labels its shelves differently.
    storage_location = models.CharField(
        max_length=200, blank=True, help_text="Where it is placed, e.g. Rack A3"
    )
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    # Picked from the dashboard's in-hand stock panel to be watched there.
    watch_on_dashboard = models.BooleanField(default=False)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["material_type__name"]

    def __str__(self):
        return f"{self.material_type.name} ({self.sku})"

    def save(self, *args, **kwargs):
        if not self.sku:
            self.sku = generate_code("inventory_item", model=type(self), field="sku")
        super().save(*args, **kwargs)

    @property
    def is_low_stock(self):
        return self.quantity <= self.min_stock_level


class InventoryUnitType(TimeStampedModel):
    """The *definition* of a unique (serialized) product.

    A unique item is "opened" here once, with all its technical details and no
    stock at all (quantity 0). Individual physical units — the serials — attach
    to it later, normally at goods-receipt inspection, where the technician
    only has to type the serial number because everything else already lives
    on this record.

    The generic counterpart needs no such split: ``InventoryItem`` is itself
    the definition and carries its own quantity.
    """

    type_code = models.CharField(max_length=50, unique=True, blank=True, db_index=True)
    name = models.CharField(max_length=200)
    material_type = models.ForeignKey(
        "assets.MaterialType", on_delete=models.PROTECT, null=True, blank=True,
        related_name="inventory_unit_types",
    )
    category = models.ForeignKey(
        InventoryCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name="unit_types"
    )
    brand = models.ForeignKey(
        "assets.Brand", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="inventory_unit_types",
    )
    model_name = models.CharField(max_length=200, blank=True)
    unit = models.CharField(max_length=50, default="piece")
    specifications = models.JSONField(
        default=dict, blank=True, help_text="Free-form technical details captured when the item is opened"
    )
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    min_stock_level = models.IntegerField(default=0)

    # Counted on the dashboard's in-hand stock figure. Off by default: which
    # products are worth watching at that level is the user's call, not a
    # threshold the system invents.
    is_high_value = models.BooleanField(
        default=False, help_text="Include this product in in-hand stock on the dashboard"
    )

    # Warranty terms that every unit of this product inherits by default.
    default_has_warranty = models.BooleanField(default=False)
    default_warranty_type = models.CharField(max_length=20, blank=True)
    default_warranty_months = models.PositiveSmallIntegerField(null=True, blank=True)

    supplier = models.ForeignKey(
        "suppliers.Supplier", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="inventory_unit_types",
    )
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        parts = [self.name]
        if self.model_name:
            parts.append(self.model_name)
        return " ".join(parts)

    def save(self, *args, **kwargs):
        if not self.type_code:
            self.type_code = generate_code("inventory_unit_type", model=type(self), field="type_code")
        super().save(*args, **kwargs)

    @property
    def in_stock_count(self) -> int:
        """How many physical units of this product are on hand right now."""
        return self.units.filter(status=InventoryUnit.Status.IN_STOCK).count()


class InventoryUnit(TimeStampedModel):
    """A **unique** (serialized) inventory item: one physical unit carrying its
    own serial number, make/model and optional warranty.

    This is the counterpart to :class:`InventoryItem`, which tracks **generic**
    stock by quantity only. Both live in the warehouse; the difference is
    whether individual units are distinguishable.

    Not to be confused with ``assets.Device``: a Device is an asset that has
    entered the project/installation lifecycle (installs, tickets, maintenance,
    the central warranty register). An InventoryUnit is warehouse stock that
    happens to be serialized. Units are promoted to Devices on registration —
    ``converted_device`` records that link.
    """

    class Status(models.TextChoices):
        IN_STOCK = "in_stock", "In Stock"
        RESERVED = "reserved", "Reserved"
        ISSUED = "issued", "Issued"
        RETURNED = "returned", "Returned"
        DAMAGED = "damaged", "Damaged"
        SCRAPPED = "scrapped", "Scrapped"
        CONVERTED = "converted", "Registered as Asset"

    class WarrantyType(models.TextChoices):
        # Mirrors warranties.Warranty.WarrantyType so the two registers can be
        # merged later without a data migration on the values themselves.
        MANUFACTURER = "manufacturer", "Manufacturer"
        EXTENDED = "extended", "Extended"
        SUPPLIER = "supplier", "Supplier"
        CLIENT = "client", "Client Warranty"

    # Explicit state machine, per the platform standard — status changes go
    # through the /transition/ action, never a free dropdown.
    VALID_TRANSITIONS = {
        Status.IN_STOCK: (Status.RESERVED, Status.ISSUED, Status.DAMAGED, Status.SCRAPPED, Status.CONVERTED),
        Status.RESERVED: (Status.IN_STOCK, Status.ISSUED, Status.DAMAGED),
        Status.ISSUED: (Status.RETURNED, Status.DAMAGED, Status.SCRAPPED),
        Status.RETURNED: (Status.IN_STOCK, Status.DAMAGED, Status.SCRAPPED),
        Status.DAMAGED: (Status.IN_STOCK, Status.SCRAPPED),
        Status.SCRAPPED: (),
        Status.CONVERTED: (),
    }

    unit_code = models.CharField(max_length=50, unique=True, blank=True, db_index=True)
    serial_number = models.CharField(max_length=200, unique=True, db_index=True)
    # The product this unit is one of. Technical details live on the type, so
    # receiving a unit only needs its serial number.
    unit_type = models.ForeignKey(
        InventoryUnitType, on_delete=models.PROTECT, null=True, blank=True, related_name="units"
    )

    # Optional: goods bought against a serialized PO line are identified by
    # brand + model rather than a material type, so a unit needs one or the
    # other (enforced in the serializer), not necessarily both.
    material_type = models.ForeignKey(
        "assets.MaterialType", on_delete=models.PROTECT, null=True, blank=True,
        related_name="inventory_units",
    )
    category = models.ForeignKey(
        InventoryCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name="units"
    )
    # "Make" and "model" as the client words them. Brand reuses the existing
    # catalogue (managed in Setup); model is free text because warehouse spares
    # are often not in the screen-centric DeviceModel catalogue.
    brand = models.ForeignKey(
        "assets.Brand", on_delete=models.SET_NULL, null=True, blank=True, related_name="inventory_units"
    )
    model_name = models.CharField(max_length=200, blank=True)

    status = models.CharField(max_length=15, choices=Status.choices, default=Status.IN_STOCK, db_index=True)
    location = models.CharField(
        max_length=15, choices=InventoryItem.Location.choices, default=InventoryItem.Location.WAREHOUSE
    )

    supplier = models.ForeignKey(
        "suppliers.Supplier", on_delete=models.SET_NULL, null=True, blank=True, related_name="inventory_units"
    )
    purchase_date = models.DateField(null=True, blank=True)
    purchase_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    batch_number = models.CharField(max_length=100, blank=True)
    goods_receipt_line = models.ForeignKey(
        "inventory.GoodsReceiptLine", on_delete=models.SET_NULL, null=True, blank=True, related_name="units"
    )

    # --- Warranty (optional: "there can be warranty or not") ---------------
    has_warranty = models.BooleanField(default=False)
    warranty_type = models.CharField(max_length=20, choices=WarrantyType.choices, blank=True)
    warranty_start = models.DateField(null=True, blank=True)
    warranty_months = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Months from start; end date is derived when not given directly"
    )
    warranty_end = models.DateField(null=True, blank=True)

    converted_device = models.OneToOneField(
        "assets.Device", on_delete=models.SET_NULL, null=True, blank=True, related_name="source_inventory_unit"
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "location"]),
            models.Index(fields=["material_type", "status"]),
        ]

    def __str__(self):
        label = self.material_type.name if self.material_type_id else (self.model_name or "unit")
        return f"{self.serial_number} ({label})"

    def save(self, *args, **kwargs):
        if not self.unit_code:
            self.unit_code = generate_code("inventory_unit", model=type(self), field="unit_code")

        # A new unit inherits everything from the product it belongs to, so
        # receiving one only needs the serial number. Anything supplied
        # explicitly wins.
        if self._state.adding and self.unit_type_id:
            product = self.unit_type
            self.material_type_id = self.material_type_id or product.material_type_id
            self.category_id = self.category_id or product.category_id
            self.brand_id = self.brand_id or product.brand_id
            self.model_name = self.model_name or product.model_name
            self.supplier_id = self.supplier_id or product.supplier_id
            if self.purchase_price is None:
                self.purchase_price = product.unit_cost
            if not self.has_warranty and product.default_has_warranty:
                self.has_warranty = True
                self.warranty_type = self.warranty_type or product.default_warranty_type
                self.warranty_months = self.warranty_months or product.default_warranty_months
                if not self.warranty_start:
                    self.warranty_start = self.purchase_date or timezone.now().date()
        # Months-only entry derives the end date, matching how warranties are
        # created elsewhere in the platform.
        if self.has_warranty and self.warranty_end is None and self.warranty_start and self.warranty_months:
            from dateutil.relativedelta import relativedelta

            self.warranty_end = self.warranty_start + relativedelta(months=self.warranty_months)
        if not self.has_warranty:
            self.warranty_type = ""
            self.warranty_start = None
            self.warranty_months = None
            self.warranty_end = None
        super().save(*args, **kwargs)

    def can_transition_to(self, new_status) -> bool:
        return new_status in self.VALID_TRANSITIONS.get(self.status, ())

    @property
    def warranty_state(self) -> str:
        """``none`` | ``active`` | ``expired`` — drives the UI badge."""
        if not self.has_warranty or not self.warranty_end:
            return "none"
        return "expired" if self.warranty_end < timezone.now().date() else "active"

    @property
    def is_under_warranty(self) -> bool:
        return self.warranty_state == "active"


class StockMovement(TimeStampedModel):
    class MovementType(models.TextChoices):
        IN = "in", "Stock In"
        OUT = "out", "Stock Out"
        TRANSFER = "transfer", "Transfer"
        ADJUSTMENT = "adjustment", "Adjustment"

    item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE, related_name="movements")
    movement_type = models.CharField(max_length=15, choices=MovementType.choices)
    quantity = models.IntegerField()
    reference = models.CharField(max_length=200, blank=True)
    # Generic stock is fungible, so the batch lives on the movement rather than
    # the item — this is what traces a quantity back to its GRN and PO.
    batch_number = models.CharField(max_length=100, blank=True, db_index=True)
    goods_receipt_line = models.ForeignKey(
        "inventory.GoodsReceiptLine", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="stock_movements",
    )
    notes = models.TextField(blank=True)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="stock_movements"
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.movement_type} {self.quantity}x {self.item.material_type.name}"


class GoodsReceipt(TimeStampedModel):
    """Receiving stock into the warehouse — optionally against a Work Order
    (legacy single-item flow) or a Purchase Order (line-level GRN, WF-04)."""

    class Source(models.TextChoices):
        PURCHASE = "purchase", "Purchase Order"
        PROJECT_RETURN = "project_return", "Returned from Project"
        MAINTENANCE_RETURN = "maintenance_return", "Returned from Maintenance"

    grn_number = models.CharField(max_length=50, unique=True, blank=True, db_index=True)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.PURCHASE)
    work_order = models.ForeignKey(
        "workorders.WorkOrder", on_delete=models.SET_NULL, null=True, blank=True, related_name="goods_receipts"
    )
    purchase_order = models.ForeignKey(
        "procurement.PurchaseOrder", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="goods_receipts",
    )
    # Legacy single-item receipt fields; PO receipts use GoodsReceiptLine rows instead.
    item = models.ForeignKey(
        InventoryItem, on_delete=models.PROTECT, null=True, blank=True, related_name="receipts"
    )
    quantity = models.PositiveIntegerField(null=True, blank=True)
    reference = models.CharField(max_length=200, blank=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="goods_receipts"
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        target = self.item or self.purchase_order or self.work_order or "receipt"
        return f"{self.grn_number} - {target}"

    def save(self, *args, **kwargs):
        if not self.grn_number:
            self.grn_number = generate_code("goods_receipt", model=type(self), field="grn_number")
        super().save(*args, **kwargs)


class GoodsReceiptLine(TimeStampedModel):
    """One received PO line on a goods receipt: quantity plus the unique
    serials/batch captured at the door (WF-04).

    Goods do **not** reach the warehouse on receipt. Each line lands
    ``pending`` inspection; a technician checks it and routes the accepted
    quantity into either generic stock (``InventoryItem``) or unique units
    (``InventoryUnit``). Only that step moves stock.
    """

    class Inspection(models.TextChoices):
        PENDING = "pending", "Pending Inspection"
        PASSED = "passed", "Passed — Stocked"
        REJECTED = "rejected", "Rejected"

    class Route(models.TextChoices):
        GENERIC = "generic", "Generic Stock"
        UNIQUE = "unique", "Unique Items"

    receipt = models.ForeignKey(GoodsReceipt, on_delete=models.CASCADE, related_name="lines")
    po_item = models.ForeignKey(
        "procurement.PurchaseOrderItem", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="receipt_lines",
    )
    inventory_item = models.ForeignKey(
        InventoryItem, on_delete=models.SET_NULL, null=True, blank=True, related_name="receipt_lines"
    )
    quantity = models.PositiveIntegerField()
    batch_number = models.CharField(max_length=100, blank=True)
    serial_numbers = models.JSONField(default=list, blank=True)

    # --- Inspection gate ---------------------------------------------------
    inspection_status = models.CharField(
        max_length=10, choices=Inspection.choices, default=Inspection.PENDING, db_index=True
    )
    routed_to = models.CharField(max_length=10, choices=Route.choices, blank=True)
    accepted_quantity = models.PositiveIntegerField(null=True, blank=True)
    rejected_quantity = models.PositiveIntegerField(default=0)
    inspected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="inspected_receipt_lines",
    )
    inspected_at = models.DateTimeField(null=True, blank=True)
    inspection_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["inspection_status", "created_at"])]

    def __str__(self):
        return f"{self.receipt.grn_number}: {self.po_item or self.inventory_item} x{self.quantity}"

    @property
    def is_pending_inspection(self) -> bool:
        return self.inspection_status == self.Inspection.PENDING


class Issuance(TimeStampedModel):
    """Issuing stock out of the warehouse to a site / work order / technician."""

    issue_number = models.CharField(max_length=50, unique=True, blank=True, db_index=True)
    item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name="issuances")
    quantity = models.PositiveIntegerField()
    issued_to_site = models.ForeignKey(
        "sites.Site", on_delete=models.SET_NULL, null=True, blank=True, related_name="inventory_issuances"
    )
    issued_to_work_order = models.ForeignKey(
        "workorders.WorkOrder", on_delete=models.SET_NULL, null=True, blank=True, related_name="inventory_issuances"
    )
    issued_to_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="received_issuances"
    )
    issued_to_project = models.ForeignKey(
        "teams.Project", on_delete=models.SET_NULL, null=True, blank=True, related_name="inventory_issuances"
    )
    bom_line = models.ForeignKey(
        "teams.ProjectBOMLine", on_delete=models.SET_NULL, null=True, blank=True, related_name="issuances"
    )
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="issued_issuances"
    )
    reason = models.CharField(max_length=300, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.issue_number} - {self.item}"

    def save(self, *args, **kwargs):
        if not self.issue_number:
            self.issue_number = generate_code("issuance", model=type(self), field="issue_number")
        super().save(*args, **kwargs)


class IssuanceRequest(TimeStampedModel):
    """Material asked of the store, and what the store has handed over.

    Everything leaves the warehouse through one desk: a project that needs
    parts for a build and a technician who needs parts for a repair both queue
    here, so stock is only ever issued by the person who actually holds it.
    A request that cannot be met in full is issued in part, and the balance
    stays on the queue.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting Issue"
        PARTIAL = "partial", "Partly Issued"
        FULFILLED = "fulfilled", "Issued"
        CANCELLED = "cancelled", "Cancelled"

    class Source(models.TextChoices):
        PROJECT = "project", "Project"
        MAINTENANCE = "maintenance", "Maintenance"
        OTHER = "other", "Other"

    request_number = models.CharField(max_length=50, unique=True, blank=True, db_index=True)

    # Generic stock or an opened serialized product — exactly one, mirroring
    # how a requirement names what it needs.
    item = models.ForeignKey(
        InventoryItem, on_delete=models.PROTECT, null=True, blank=True,
        related_name="issuance_requests",
    )
    unit_type = models.ForeignKey(
        InventoryUnitType, on_delete=models.PROTECT, null=True, blank=True,
        related_name="issuance_requests",
    )

    quantity_requested = models.PositiveIntegerField()
    quantity_issued = models.PositiveIntegerField(default=0)

    source = models.CharField(max_length=12, choices=Source.choices, default=Source.OTHER)
    purpose = models.CharField(max_length=300, blank=True)

    # What the material is for. A project build points at the requirement it
    # covers, so issuing here moves that requirement along.
    project = models.ForeignKey(
        "teams.Project", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="issuance_requests",
    )
    asset_component = models.ForeignKey(
        "assets.AssetComponent", on_delete=models.CASCADE, null=True, blank=True,
        related_name="issuance_requests",
    )
    maintenance_schedule = models.ForeignKey(
        "maintenance.MaintenanceSchedule", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="issuance_requests",
    )

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="material_requests",
    )
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="material_requests_issued",
    )
    received_by = models.CharField(
        max_length=200, blank=True, help_text="Who took the material away"
    )
    # Serials handed over, in the order they went out — the issue slip lists them.
    issued_serials = models.JSONField(default=list, blank=True)

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.request_number} - {self.what}"

    @property
    def what(self):
        """What is being asked for, however it was specified."""
        if self.item_id:
            return str(self.item)
        if self.unit_type_id:
            return str(self.unit_type)
        return "—"

    @property
    def outstanding_quantity(self) -> int:
        return max(self.quantity_requested - self.quantity_issued, 0)

    @property
    def available_quantity(self):
        """What the warehouse can actually cover right now."""
        if self.item_id:
            return self.item.quantity
        if self.unit_type_id:
            return self.unit_type.in_stock_count
        return None

    def sync_status(self):
        """Status follows the quantities; a cancelled request stays cancelled."""
        if self.status == self.Status.CANCELLED:
            return
        if self.quantity_issued == 0:
            self.status = self.Status.PENDING
        elif self.outstanding_quantity > 0:
            self.status = self.Status.PARTIAL
        else:
            self.status = self.Status.FULFILLED

    def save(self, *args, **kwargs):
        if not self.request_number:
            self.request_number = generate_code(
                "material_request", model=type(self), field="request_number"
            )
        super().save(*args, **kwargs)
