from django.conf import settings
from django.db import models

from common.models import TimeStampedModel
from common.utils import upload_to_path


class Site(TimeStampedModel):
    name = models.CharField(max_length=300)
    address = models.TextField()
    city = models.CharField(max_length=100, blank=True)
    state_province = models.CharField(max_length=150, blank=True, help_text="State, province, or emirate")
    country = models.CharField(max_length=100, default="Pakistan")
    latitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    longitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    contact_person = models.CharField(max_length=200, blank=True)
    contact_phone = models.CharField(max_length=20, blank=True)
    contact_email = models.EmailField(blank=True)
    access_instructions = models.TextField(blank=True)
    operating_hours = models.CharField(max_length=200, blank=True)
    client = models.ForeignKey(
        "clients.Client", on_delete=models.CASCADE, related_name="client_sites", null=True, blank=True
    )
    floor_plan = models.ImageField(upload_to=upload_to_path, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class SiteContact(TimeStampedModel):
    """Point-of-contact (POC) at a site — name and contact details."""

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="contacts")
    name = models.CharField(max_length=200)
    designation = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    is_primary = models.BooleanField(default=False)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-is_primary", "name"]

    def __str__(self):
        return f"{self.name} @ {self.site.name}"


class SiteZone(TimeStampedModel):
    """Named zone/position within a site (e.g. Entrance, Aisle 3)."""

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="zones")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    floor = models.CharField(max_length=50, blank=True)

    class Meta:
        unique_together = ["site", "name"]

    def __str__(self):
        return f"{self.site.name} - {self.name}"


class DeviceInstallation(TimeStampedModel):
    """Records a device being installed at a specific site zone."""

    device = models.ForeignKey("assets.Device", on_delete=models.CASCADE, related_name="installations")
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="installations")
    zone = models.ForeignKey(SiteZone, on_delete=models.SET_NULL, null=True, blank=True, related_name="installations")
    installed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="installations_done"
    )
    installed_at = models.DateTimeField()
    removed_at = models.DateTimeField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True, help_text="Agreed completion date for the installation")
    completed_at = models.DateTimeField(
        null=True, blank=True, help_text="Auto-stamped when every step is completed or skipped"
    )
    position_label = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-installed_at"]

    def __str__(self):
        return f"{self.device.asset_code} @ {self.site.name}"


class InstallationStep(TimeStampedModel):
    """Tracks individual steps in a device installation pipeline."""

    class StepType(models.TextChoices):
        SURVEY = "survey", "Survey"
        WIRING = "wiring", "Wiring"
        STRUCTURE = "structure", "Metal Structure Installation"
        PROGRAMMING = "programming", "Programming"
        TESTING = "testing", "Testing & Commissioning"
        HANDOVER = "handover", "Handover"

    class StepStatus(models.TextChoices):
        NOT_STARTED = "not_started", "Not Started Yet"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETED = "completed", "Completed"
        SKIPPED = "skipped", "Skipped"

    installation = models.ForeignKey(
        DeviceInstallation, on_delete=models.CASCADE, related_name="steps"
    )
    step_type = models.CharField(max_length=20, choices=StepType.choices)
    step_number = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=20, choices=StepStatus.choices, default=StepStatus.NOT_STARTED)
    assigned_team = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["step_number"]
        unique_together = ["installation", "step_number"]

    def save(self, *args, **kwargs):
        from django.utils import timezone

        # Stamp the timeline automatically as the step advances.
        if self.status == self.StepStatus.IN_PROGRESS and not self.started_at:
            self.started_at = timezone.now()
        if self.status == self.StepStatus.COMPLETED:
            if not self.started_at:
                self.started_at = timezone.now()
            if not self.completed_at:
                self.completed_at = timezone.now()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.installation} - Step {self.step_number}: {self.get_step_type_display()}"


class InstallationDelay(TimeStampedModel):
    """A logged delay during an installation, attributable to a cause (esp. the client)."""

    class Cause(models.TextChoices):
        CLIENT = "client", "Client"
        INTERNAL = "internal", "Internal"
        VENDOR = "vendor", "Vendor"
        OTHER = "other", "Other"

    installation = models.ForeignKey(
        DeviceInstallation, on_delete=models.CASCADE, related_name="delays"
    )
    step = models.ForeignKey(
        InstallationStep, on_delete=models.CASCADE, null=True, blank=True, related_name="delays"
    )
    cause = models.CharField(max_length=15, choices=Cause.choices)
    description = models.TextField(blank=True)
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="installation_delays_reported"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_cause_display()} delay on {self.installation}"


class InstallationPhoto(TimeStampedModel):
    class PhotoType(models.TextChoices):
        PRE_INSTALL = "pre_install", "Pre-Installation"
        POST_INSTALL = "post_install", "Post-Installation"
        VERIFICATION = "verification", "Verification"

    installation = models.ForeignKey(
        DeviceInstallation, on_delete=models.CASCADE, related_name="photos"
    )
    photo_type = models.CharField(max_length=20, choices=PhotoType.choices)
    image = models.ImageField(upload_to=upload_to_path)
    caption = models.CharField(max_length=300, blank=True)
    latitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    longitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    taken_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True
    )

    def __str__(self):
        return f"{self.photo_type} - {self.installation}"
