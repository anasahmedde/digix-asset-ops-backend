from rest_framework.permissions import BasePermission


class IsSuperAdmin(BasePermission):
    """Only super admins (is_superuser) can access."""

    def has_permission(self, request, view):
        return request.user and request.user.is_superuser


class IsOperationsManager(BasePermission):
    def has_permission(self, request, view):
        return request.user and hasattr(request.user, "role") and request.user.role in ("super_admin", "ops_manager")


class IsTechnician(BasePermission):
    def has_permission(self, request, view):
        return request.user and hasattr(request.user, "role") and request.user.role in (
            "super_admin",
            "ops_manager",
            "technician",
        )


class IsFinance(BasePermission):
    def has_permission(self, request, view):
        return request.user and hasattr(request.user, "role") and request.user.role in (
            "super_admin",
            "finance",
        )
