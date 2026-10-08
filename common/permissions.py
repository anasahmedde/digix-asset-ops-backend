"""Who may do what.

Every gate is a capability (``apps.accounts.capabilities``). A view names the
capability each of its actions needs and ``CapabilityGate`` checks it; the
helpers below answer the two questions a capability alone cannot, because
they are about the organogram rather than about rights:

* ``can_for_project``: a project's manager decides the project's own matters.
* ``decides_for``: a reviewer decides for their own reports, or for everybody
  when they hold ``act_across_teams``.

The role tuples at the bottom are kept for the few places that still read a
role name (seeding, scoping of what a vendor or client login may *see*).
Nothing that grants a right should be added to them.
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.accounts.capabilities import PROJECT_MANAGER_CAPABILITIES

# Platform administration fallback: a superuser account always passes.
ADMIN_ROLES = ("super_admin",)
# Historic groupings, kept only for read-scoping and seed data.
MANAGER_ROLES = ("super_admin", "group_head", "ops_manager")
SUPERVISOR_ROLES = ("super_admin", "group_head", "ops_manager", "supervisor")
COMMERCIAL_ROLES = ("super_admin", "group_head", "ops_manager", "marketing_head")
FIELD_ROLES = ("super_admin", "group_head", "ops_manager", "supervisor", "technician")
FINANCE_ROLES = ("super_admin", "group_head", "finance")
WAREHOUSE_ROLES = ("super_admin", "group_head", "ops_manager", "warehouse")
ISSUING_ROLES = ("super_admin", "ops_manager", "warehouse")
ALL_INTERNAL_ROLES = (
    "super_admin", "group_head", "ops_manager", "marketing_head", "supervisor",
    "technician", "finance", "warehouse",
)
# External logins: everything they see is scoped to their own supplier or client.
VENDOR_ROLES = ("vendor",)
EXTERNAL_ROLES = ("vendor", "client_viewer")


def can(user, capability) -> bool:
    """Does this person hold the capability? Superusers always do."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    check = getattr(user, "can", None)
    return bool(capability and callable(check) and check(capability))


_can = can  # the older name, still imported in places


def can_any(user, *capabilities) -> bool:
    return any(can(user, c) for c in capabilities)


def is_project_manager(user, project) -> bool:
    return bool(project is not None and user is not None and project.manager_id == user.pk)


def can_for_project(user, project, capability) -> bool:
    """Hold the capability, or be the manager of this project and it is one
    of the things a project's manager decides."""
    if can(user, capability):
        return True
    return capability in PROJECT_MANAGER_CAPABILITIES and is_project_manager(user, project)


def acts_across_teams(user) -> bool:
    return can(user, "act_across_teams")


def decides_for(user, worker, capability) -> bool:
    """May this person decide on work ``worker`` did or asked for?

    Holding the capability is the first half. The second is the organogram:
    a supervisor reviews their own crew, not another supervisor's, unless
    they act across teams. With no worker named there is nobody to be
    scoped to, so the capability alone decides.
    """
    if not can(user, capability):
        return False
    if worker is None or worker.pk == user.pk or acts_across_teams(user):
        return True
    manages = getattr(user, "manages", None)
    return bool(callable(manages) and manages(worker))


class CapabilityGate(BasePermission):
    """The one permission class every view uses.

    A view names what each half, or each action, needs::

        class ProjectViewSet(...):
            permission_classes = [IsAuthenticated, CapabilityGate]
            read_capability = "view_projects"
            write_capability = "edit_projects"
            action_capabilities = {"approve_budget": "approve_budget"}

    ``action_capabilities`` wins for the action it names; otherwise a safe
    method needs ``read_capability`` and anything else ``write_capability``.
    A half that names nothing is open to any signed-in person - the view
    then decides inside the action, where it knows the object.
    """

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        # Self-service always works: a person can read their own profile
        # and change their own password whatever else they may not see.
        action = getattr(view, "action", None)
        if action in getattr(view, "capability_exempt_actions", ("me", "change_password")):
            return True
        per_action = getattr(view, "action_capabilities", None) or {}
        if action in per_action:
            needed = per_action[action]
            if needed is None:
                return True
            return can_any(user, *needed) if isinstance(needed, (tuple, list, set)) else can(user, needed)
        needed = (
            getattr(view, "read_capability", None)
            if request.method in SAFE_METHODS
            else getattr(view, "write_capability", None)
        )
        if not needed:
            return True
        return can_any(user, *needed) if isinstance(needed, (tuple, list, set)) else can(user, needed)


def _role(user):
    return getattr(user, "role", None)


class IsSuperAdmin(BasePermission):
    """Logins: the Super Admin, or anyone given ``manage_logins``."""

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return request.user.is_superuser or _role(request.user) in ADMIN_ROLES or can(request.user, "manage_logins")


# ---------------------------------------------------------------------------
# The classes below used to carry a role list each. They now read the
# capability their module needs, so a role written on the screen works
# through them too. New views should name capabilities directly.
# ---------------------------------------------------------------------------

class _CapabilityWriteElseRead(BasePermission):
    write_capabilities: tuple[str, ...] = ()

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method in SAFE_METHODS:
            return True
        return can_any(request.user, *self.write_capabilities)


class IsAdminOrManager(_CapabilityWriteElseRead):
    write_capabilities = ("manage_setup",)

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return can_any(request.user, *self.write_capabilities)


class AdminManagerWriteElseRead(_CapabilityWriteElseRead):
    """Reference data that Operations maintains; everyone else reads it."""
    write_capabilities = ("manage_setup",)


class FinanceWriteElseRead(_CapabilityWriteElseRead):
    write_capabilities = ("manage_finance",)


class WarehouseWriteElseRead(_CapabilityWriteElseRead):
    write_capabilities = ("manage_stock",)


class PurchaseOrderActionElseRead(_CapabilityWriteElseRead):
    write_capabilities = ("raise_po", "approve_po", "cancel_po")


class InspectionWriteElseRead(_CapabilityWriteElseRead):
    write_capabilities = ("inspect_goods", "receive_goods")


class CommercialWriteElseRead(_CapabilityWriteElseRead):
    write_capabilities = ("manage_clients", "manage_sites", "manage_quotations", "manage_setup")


class TechnicianCanCreate(BasePermission):
    """Tickets and maintenance: anyone who may work them may write; the
    object-level rules inside each action say which record."""

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method in SAFE_METHODS:
            return True
        return can_any(
            request.user, "work_tickets", "raise_ticket", "assign_ticket",
            "work_maintenance", "manage_maintenance",
        )


class IsOperationsManager(BasePermission):
    def has_permission(self, request, view):
        return can(request.user, "act_across_teams")


class IsTechnician(BasePermission):
    def has_permission(self, request, view):
        return can_any(request.user, "work_tickets", "edit_installation", "work_maintenance")


class IsFinance(BasePermission):
    def has_permission(self, request, view):
        return can(request.user, "manage_finance")
