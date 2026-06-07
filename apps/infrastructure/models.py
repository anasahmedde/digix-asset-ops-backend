from django.conf import settings
from django.db import models

from common.models import TimeStampedModel
from common.utils import upload_to_path


class Document(TimeStampedModel):
    class DocType(models.TextChoices):
        REPORT = "report", "Report"
        SPECIFICATION = "specification", "Specification"
        DRAWING = "drawing", "Drawing / Diagram"
        MANUAL = "manual", "Manual"
        CONTRACT = "contract", "Contract"
        INVOICE = "invoice", "Invoice"
        PHOTO = "photo", "Photo"
        OTHER = "other", "Other"

    title = models.CharField(max_length=300)
    doc_type = models.CharField(max_length=20, choices=DocType.choices, default=DocType.OTHER)
    file = models.FileField(upload_to=upload_to_path)
    file_size = models.PositiveIntegerField(default=0, help_text="Size in bytes")
    description = models.TextField(blank=True)

    device = models.ForeignKey(
        "assets.Device", on_delete=models.CASCADE, null=True, blank=True, related_name="documents"
    )
    site = models.ForeignKey(
        "sites.Site", on_delete=models.CASCADE, null=True, blank=True, related_name="documents"
    )
    project = models.ForeignKey(
        "teams.Project", on_delete=models.CASCADE, null=True, blank=True, related_name="documents"
    )
    installation = models.ForeignKey(
        "sites.DeviceInstallation", on_delete=models.CASCADE, null=True, blank=True, related_name="documents"
    )

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="uploaded_documents"
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title
