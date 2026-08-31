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
    """Type of material/component used across the platform (cables, mounts, etc.)."""

    name = models.CharField(max_length=200, unique=True)
    category = models.CharField(max_length=100, blank=True)
    unit = models.CharField(max_length=50, default="piece")
    description = models.TextField(blank=True)

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
        PROCURED = "procured", "Procured"
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
        INHOUSE = "inhouse", "In-house Production"
        THIRD_PARTY = "third_party", "Third Party"

    # Enforced status machine — status changes go through the /transition/
    # action (see views.DeviceViewSet.transition); every flip is journalled
    # as a DeviceLifecycleEvent + AuditLog entry by signals.py.
    VALID_TRANSITIONS = {
        Status.PROCURED: (Status.IN_PRODUCTION, Status.IN_STOCK, Status.IN_TRANSIT, Status.RMA),
        Status.IN_PRODUCTION: (Status.IN_STOCK, Status.RMA),
        Status.IN_TRANSIT: (Status.IN_STOCK, Status.PROCURED),
        Status.IN_STOCK: (
            Status.ASSIGNED, Status.IN_PRODUCTION, Status.IN_TRANSIT,
            Status.DECOMMISSIONED, Status.LOST_STOLEN,
        ),
        Status.ASSIGNED: (Status.INSTALLED, Status.IN_STOCK, Status.IN_TRANSIT),
        Status.INSTALLED: (Status.ACTIVE, Status.UNDER_MAINTENANCE, Status.ASSIGNED, Status.RMA),
        Status.ACTIVE: (
            Status.UNDER_MAINTENANCE, Status.RMA, Status.CLIENT_PROPERTY,
            Status.IN_TRANSIT, Status.DECOMMISSIONED, Status.LOST_STOLEN,
        ),
        Status.UNDER_MAINTENANCE: (Status.ACTIVE, Status.INSTALLED, Status.RMA, Status.DECOMMISSIONED),
        Status.RMA: (Status.IN_STOCK, Status.DECOMMISSIONED),
        Status.CLIENT_PROPERTY: (Status.DECOMMISSIONED,),
        Status.LOST_STOLEN: (Status.IN_STOCK,),
        Status.DECOMMISSIONED: (),
    }

    asset_code = models.CharField(max_length=50, unique=True, db_index=True)
    serial_number = models.CharField(max_length=200, unique=True)
    mobile_id = models.CharField(max_length=200, blank=True, help_text="Linked CMS device ID")
    mac_address = models.CharField(max_length=17, blank=True)
    imei = models.CharField(max_length=20, blank=True)

    asset_type = models.ForeignKey(
        AssetType, on_delete=models.PROTECT, null=True, blank=True, related_name="devices",
        help_text="Asset category (SMD Screen, Standee, Digital Display, …)",
    )
    device_model = models.ForeignKey(DeviceModel, on_delete=models.PROTECT, related_name="devices")
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
        max_length=20, choices=Source.choices, default=Source.THIRD_PARTY, db_index=True
    )

    image = models.ImageField(upload_to=upload_to_path, blank=True, help_text="Primary device photo")

    purchase_date = models.DateField(null=True, blank=True)
    purchase_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
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
    assigned_technician = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_devices"
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
        return f"{self.asset_code} ({self.device_model})"

    def save(self, *args, **kwargs):
        if not self.asset_code:
            self.asset_code = generate_code("asset", model=type(self), field="asset_code")
        super().save(*args, **kwargs)

    def can_transition_to(self, new_status: str) -> bool:
        allowed = self.VALID_TRANSITIONS.get(self.status, ())
        return new_status in allowed


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
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.device.asset_code} · {self.name} ×{self.quantity}"


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
