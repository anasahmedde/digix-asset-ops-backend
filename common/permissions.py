from rest_framework.permissions import SAFE_METHODS, BasePermission

# Platform administration: user accounts, roles, teams, audit log. Per the
# client's signed authority matrix this is the Super Admin alone.
ADMIN_ROLES = ("super_admin",)
# Group Head is the escalation apex (oversees operations AND marketing) and
# carries full manager powers.
MANAGER_ROLES = ("super_admin", "group_head", "ops_manager")
# Supervisors sit between managers and technicians: they run field crews,
# can act on tickets and review/approve their team's work.
SUPERVISOR_ROLES = ("super_admin", "group_head", "ops_manager", "supervisor")
# The client-facing lead runs the commercial side alongside management.
COMMERCIAL_ROLES = ("super_admin", "group_head", "ops_manager", "marketing_head")
FIELD_ROLES = ("super_admin", "group_head", "ops_manager", "supervisor", "technician")
# No Finance position exists on the org chart, so the Group Head carries the
# finance authority (signed off with the organogram).
FINANCE_ROLES = ("super_admin", "group_head", "finance")
WAREHOUSE_ROLES = ("super_admin", "group_head", "ops_manager", "warehouse")
# Who physically hands material over against a request (approval gate 3).
ISSUING_ROLES = ("super_admin", "ops_manager", "warehouse")
ALL_INTERNAL_ROLES = (
    "super_admin", "group_head", "ops_manager", "marketing_head", "supervisor",
    "technician", "finance", "warehouse",
)
# External vendor-portal logins (XC-04). Deliberately absent from every
# write-role group above: vendors are read-only everywhere except the
# explicit ticket/installation actions scoped to their own supplier.
VENDOR_ROLES = ("vendor",)


def _role(user):
    return getattr(user, "role", None)


class IsSuperAdmin(BasePermission):
    """Platform administration — the Super Admin (see ADMIN_ROLES)."""

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
    """Finance, Operations and Admin can write; everyone else is read-only."""

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method in SAFE_METHODS:
            return True
        # Procurement rights sit with Operations (client decision 2); the Group
        # Head's lever is approving the budget that unlocks it, and signing
        # off the order itself — see PurchaseOrderViewSet.transition.
        return _role(request.user) in ("super_admin", "ops_manager", "finance")


class WarehouseWriteElseRead(BasePermission):
    """Warehouse + Admin/Manager can write; everyone else is read-only."""

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method in SAFE_METHODS:
            return True
        return _role(request.user) in WAREHOUSE_ROLES


class PurchaseOrderActionElseRead(BasePermission):
    """Moving a purchase order along: Operations/Finance/Admin raise and place
    orders, and the Group Head signs them (the view limits the Group Head to
    that step). Everyone authenticated may read."""

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method in SAFE_METHODS:
            return True
        return _role(request.user) in ("super_admin", "ops_manager", "finance", "group_head")


class InspectionWriteElseRead(BasePermission):
    """Goods-receipt inspection: supervisors check deliveries, the store
    receives them, management can do either. Everyone authenticated may read."""

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.method in SAFE_METHODS:
            return True
        return _role(request.user) in set(SUPERVISOR_ROLES) | set(WAREHOUSE_ROLES)


class CommercialWriteElseRead(BasePermission):
    """Management and the Marketing Head can write; everyone else is read-only."""

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method in SAFE_METHODS:
            return True
        return _role(request.user) in COMMERCIAL_ROLES


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
    # Vendors work tickets assigned to their supplier but never create or
    # edit them; the view's object gates enforce the supplier match.
    VENDOR_ALLOWED_ACTIONS = (
        "transition", "submit_completion", "ticket_comments",
    )

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method in SAFE_METHODS:
            return True
        role = _role(request.user)
        if role in MANAGER_ROLES or role == "marketing_head":
            return True
        if role in ("technician", "supervisor", "marketing") and view.action in self.TECHNICIAN_ALLOWED_ACTIONS:
            return True
        if role in VENDOR_ROLES and view.action in self.VENDOR_ALLOWED_ACTIONS:
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
