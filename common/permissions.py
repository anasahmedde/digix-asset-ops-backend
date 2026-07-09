from rest_framework.permissions import SAFE_METHODS, BasePermission

ADMIN_ROLES = ("super_admin",)
MANAGER_ROLES = ("super_admin", "ops_manager")
# Supervisors sit between managers and technicians: they run field crews,
# can act on tickets and review/approve their team's work.
SUPERVISOR_ROLES = ("super_admin", "ops_manager", "supervisor")
FIELD_ROLES = ("super_admin", "ops_manager", "supervisor", "technician")
FINANCE_ROLES = ("super_admin", "finance")
WAREHOUSE_ROLES = ("super_admin", "ops_manager", "warehouse")
ALL_INTERNAL_ROLES = (
    "super_admin", "ops_manager", "supervisor", "technician", "finance", "warehouse",
)


def _role(user):
    return getattr(user, "role", None)


class IsSuperAdmin(BasePermission):
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return request.user.is_superuser or _role(request.user) in ADMIN_ROLES


class IsAdminOrManager(BasePermission):
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return _role(request.user) in MANAGER_ROLES


class AdminManagerWriteElseRead(BasePermission):
    """Admin/Manager can do anything; everyone else is read-only."""

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method in SAFE_METHODS:
            return True
        return _role(request.user) in MANAGER_ROLES


class FinanceWriteElseRead(BasePermission):
    """Finance + Admin can write; everyone else is read-only."""

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method in SAFE_METHODS:
            return True
        return _role(request.user) in ("super_admin", "ops_manager", "finance")


class WarehouseWriteElseRead(BasePermission):
    """Warehouse + Admin/Manager can write; everyone else is read-only."""

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method in SAFE_METHODS:
            return True
        return _role(request.user) in WAREHOUSE_ROLES


class TechnicianCanCreate(BasePermission):
    """
    Admin/Manager full access. Technicians can list, retrieve, create,
    and use ticket workflow actions (transition, submit-completion,
    comments, attachments).
    Everyone else is read-only.
    """

    TECHNICIAN_ALLOWED_ACTIONS = (
        "create", "partial_update", "update",
        "transition", "submit_completion",
        "ticket_comments", "ticket_attachments",
    )

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method in SAFE_METHODS:
            return True
        role = _role(request.user)
        if role in MANAGER_ROLES:
            return True
        if role in ("technician", "supervisor", "marketing") and view.action in self.TECHNICIAN_ALLOWED_ACTIONS:
            return True
        return False


class IsOperationsManager(BasePermission):
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated and _role(request.user) in MANAGER_ROLES


class IsTechnician(BasePermission):
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated and _role(request.user) in FIELD_ROLES


class IsFinance(BasePermission):
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated and _role(request.user) in FINANCE_ROLES
