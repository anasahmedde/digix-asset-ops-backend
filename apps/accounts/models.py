import uuid

from django.contrib.auth.models import AbstractUser
from django.core.validators import RegexValidator
from django.db import models

from common.models import TimeStampedModel


class User(AbstractUser):
    class Role(models.TextChoices):
        SUPER_ADMIN = "super_admin", "Super Admin"
        GROUP_HEAD = "group_head", "Group Head"
        OPS_MANAGER = "ops_manager", "Operations Head"
        MARKETING_HEAD = "marketing_head", "Marketing Head"
        SUPERVISOR = "supervisor", "Supervisor"
        TECHNICIAN = "technician", "Technician"
        MARKETING = "marketing", "Marketing"
        FINANCE = "finance", "Finance"
        WAREHOUSE = "warehouse", "Warehouse Staff"
        CLIENT_VIEWER = "client_viewer", "Client Viewer"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.TECHNICIAN)
    # Display title within a role tier, e.g. "Production Supervisor" vs
    # "Execution Supervisor" — permissions stay on `role`.
    job_title = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    avatar = models.ImageField(upload_to="avatars/", blank=True)
    is_field_staff = models.BooleanField(default=False)
    # HR fields (EM-01): company employee number, national ID, employment dates.
    employee_id = models.CharField(max_length=50, blank=True, db_index=True)
    cnic = models.CharField(
        max_length=15,
        blank=True,
        validators=[RegexValidator(r"^\d{5}-\d{7}-\d$", "CNIC must be in #####-#######-# format")],
    )
    join_date = models.DateField(null=True, blank=True)
    leaving_date = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-date_joined"]

    def __str__(self):
        return f"{self.get_full_name()} ({self.role})"


class AuditLog(TimeStampedModel):
    class Action(models.TextChoices):
        CREATE = "create", "Create"
        UPDATE = "update", "Update"
        DELETE = "delete", "Delete"
        LOGIN = "login", "Login"
        LOGOUT = "logout", "Logout"
        EXPORT = "export", "Export"

    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="audit_logs")
    action = models.CharField(max_length=10, choices=Action.choices)
    resource_type = models.CharField(max_length=100)
    resource_id = models.CharField(max_length=100, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["resource_type", "resource_id"]),
            models.Index(fields=["user", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.user} {self.action} {self.resource_type}"


class CredentialVault(TimeStampedModel):
    """Encrypted storage for device passwords and access credentials."""

    device = models.ForeignKey(
        "assets.Device", on_delete=models.CASCADE, related_name="credentials"
    )
    label = models.CharField(max_length=100)
    username = models.CharField(max_length=255, blank=True)
    encrypted_password = models.TextField()
    notes = models.TextField(blank=True)
    last_rotated = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)

    def __str__(self):
        return f"{self.label} - {self.device}"
