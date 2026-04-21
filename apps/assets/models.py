from django.conf import settings
from django.db import models

from common.models import TimeStampedModel
from common.utils import generate_asset_code, upload_to_path


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


class Device(TimeStampedModel):
    class Status(models.TextChoices):
        PROCURED = "procured", "Procured"
        IN_STOCK = "in_stock", "In Stock"
        ASSIGNED = "assigned", "Assigned to Client"
        INSTALLED = "installed", "Installed"
        ACTIVE = "active", "Active"
        UNDER_MAINTENANCE = "under_maintenance", "Under Maintenance"
        DECOMMISSIONED = "decommissioned", "Decommissioned"
        LOST_STOLEN = "lost_stolen", "Lost/Stolen"
        RMA = "rma", "RMA"
        IN_TRANSIT = "in_transit", "In Transit"

    asset_code = models.CharField(max_length=50, unique=True, db_index=True)
    serial_number = models.CharField(max_length=200, unique=True)
    mobile_id = models.CharField(max_length=200, blank=True, help_text="Linked CMS device ID")
    mac_address = models.CharField(max_length=17, blank=True)
    imei = models.CharField(max_length=20, blank=True)

    device_model = models.ForeignKey(DeviceModel, on_delete=models.PROTECT, related_name="devices")
    firmware_version = models.CharField(max_length=100, blank=True)
    hardware_revision = models.CharField(max_length=100, blank=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PROCURED)

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
    assigned_client = models.ForeignKey(
        "clients.Client", on_delete=models.SET_NULL, null=True, blank=True, related_name="devices"
    )
    assigned_technician = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_devices"
    )
    installation_date = models.DateField(null=True, blank=True)

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
            self.asset_code = generate_asset_code()
        super().save(*args, **kwargs)


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
