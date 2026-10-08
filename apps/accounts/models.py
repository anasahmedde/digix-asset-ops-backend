import uuid

from django.conf import settings
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
        VENDOR = "vendor", "Vendor"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Not restricted to Role.choices: roles are records now, and a custom
    # one is as real as a built-in. The choices stay for the admin site and
    # for the labels the built-ins are known by.
    role = models.CharField(max_length=50, default=Role.TECHNICIAN)
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
    # The reporting line, as the organogram draws it. Permissions come from
    # `role`; this is who the person answers to, which is a different
    # question and the one the org chart asks.
    reports_to = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="direct_reports",
    )
    # Client-portal accounts: which client this login belongs to. A
    # client_viewer without one sees nothing rather than everything — an
    # external person with no client attached is a misconfiguration, and
    # the safe reading of it is "no access", not "all access".
    client = models.ForeignKey(
        "clients.Client",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="portal_users",
        help_text="For client_viewer logins: the client whose records they may see.",
    )
    # Vendor-portal accounts (XC-04): which supplier this login belongs to.
    # Everything a role=vendor user can see/do is scoped to this supplier.
    supplier = models.ForeignKey(
        "suppliers.Supplier",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="portal_users",
    )

    class Meta:
        ordering = ["-date_joined"]

    def __str__(self):
        return f"{self.get_full_name()} ({self.role})"

    # ---- What this person may do ----------------------------------------

    @property
    def capabilities(self) -> frozenset[str]:
        """Everything this person may do: the role's defaults, adjusted.

        An override is absolute — granted means granted even if the role
        would not, withdrawn means withdrawn even if it would.
        """
        from .capabilities import RENAMED
        from .roles import defaults_for

        allowed = set(defaults_for(self.role))
        for row in self.capability_overrides.all():
            keys = RENAMED.get(row.capability, (row.capability,))
            if row.allowed:
                allowed.update(keys)
            else:
                allowed.difference_update(keys)
        return frozenset(allowed)

    def can(self, capability: str) -> bool:
        return capability in self.capabilities

    def manages(self, other) -> bool:
        """Is this person somewhere up the other's reporting line?

        A team lead may adjust their own team, however deep it runs, which
        is what makes the delegation useful rather than decorative.
        """
        seen = set()
        boss = other.reports_to
        while boss is not None and boss.pk not in seen:
            if boss.pk == self.pk:
                return True
            seen.add(boss.pk)
            boss = boss.reports_to
        return False


class RoleDefinition(TimeStampedModel):
    """A role, and everything somebody holding it may do.

    The eleven built-in roles are seeded from the code defaults and can be
    adjusted from the screen; new ones can be written outright. A built-in
    is never deleted — accounts hold its key, and losing the record would
    take their rights with it.
    """

    key = models.SlugField(max_length=50, unique=True)
    label = models.CharField(max_length=100)
    description = models.CharField(max_length=300, blank=True)
    # The capability keys this role grants, as a plain list. Small, read
    # constantly, and never joined against — a table would earn nothing.
    capabilities = models.JSONField(default=list, blank=True)
    is_builtin = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="roles_created",
    )

    class Meta:
        ordering = ["-is_builtin", "label"]

    def __str__(self):
        return self.label

    @property
    def holders(self) -> int:
        from django.contrib.auth import get_user_model

        return get_user_model().objects.filter(role=self.key).count()

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        from .roles import forget

        forget()

    def delete(self, *args, **kwargs):
        from .roles import forget

        result = super().delete(*args, **kwargs)
        forget()
        return result


class UserCapability(TimeStampedModel):
    """One capability granted to, or withdrawn from, one person.

    Rows exist only where somebody differs from their role, so the table
    stays small and every row is a decision a person made and can explain.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="capability_overrides",
    )
    capability = models.CharField(max_length=50)
    allowed = models.BooleanField()
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="capabilities_granted",
    )
    reason = models.CharField(max_length=300, blank=True)

    class Meta:
        unique_together = [("user", "capability")]
        ordering = ["capability"]
        verbose_name_plural = "user capabilities"

    def __str__(self):
        verb = "granted" if self.allowed else "withdrawn"
        return f"{self.capability} {verb} for {self.user}"


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
