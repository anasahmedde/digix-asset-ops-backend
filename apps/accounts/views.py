from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import SAFE_METHODS, BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.views import TokenObtainPairView

from common.permissions import ADMIN_ROLES, IsSuperAdmin

from .models import AuditLog, UserCapability
from .serializers import (
    AuditLogSerializer,
    CapabilitySetSerializer,
    UserCreateSerializer,
    UserSerializer,
)

User = get_user_model()


class CustomTokenObtainPairView(TokenObtainPairView):
    """
    Custom login view that returns specific error messages for
    non-existent users, deactivated accounts, and wrong passwords.
    """

    def post(self, request, *args, **kwargs):
        username = request.data.get("username", "")
        password = request.data.get("password", "")

        if not username or not password:
            return Response(
                {"detail": "Username and password are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            return Response(
                {"detail": "No account found with this username."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not user.is_active:
            return Response(
                {"detail": "This account has been deactivated. Please contact your administrator."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not user.check_password(password):
            return Response(
                {"detail": "Incorrect password."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            return super().post(request, *args, **kwargs)
        except (InvalidToken, TokenError) as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_401_UNAUTHORIZED,
            )


class IsSelfOrSuperAdmin(BasePermission):
    """Writes may only target the requester's own record, unless super_admin."""

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        user = request.user
        if user.is_superuser or getattr(user, "role", None) in ADMIN_ROLES:
            return True
        # Setting somebody's capabilities has its own, finer gate: a team
        # lead may do it for their own team, which this rule cannot express.
        if getattr(view, "action", None) == "capabilities":
            return True
        return obj.pk == user.pk


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    permission_classes = [IsAuthenticated, IsSelfOrSuperAdmin]
    filterset_fields = ["role", "is_active", "is_field_staff"]
    search_fields = ["username", "email", "first_name", "last_name"]
    ordering_fields = ["date_joined", "username"]

    def get_serializer_class(self):
        if self.action == "create":
            return UserCreateSerializer
        return UserSerializer

    @action(detail=True, methods=["get", "put"], url_path="capabilities")
    def capabilities(self, request, pk=None):
        """What this person may do, and the adjustments behind it.

        GET is open to anyone who may see the team — knowing what a
        colleague is allowed to do is how you know who to ask. PUT is the
        gated half.
        """
        subject = self.get_object()

        if request.method == "GET":
            from .capabilities import defaults_for

            allowed, why = may_set_capabilities(request.user, subject)
            return Response({
                "user": str(subject.pk),
                "role": subject.role,
                "role_defaults": sorted(defaults_for(subject.role)),
                "effective": sorted(subject.capabilities),
                "overrides": [
                    {
                        "capability": row.capability,
                        "allowed": row.allowed,
                        "reason": row.reason,
                        "granted_by": (
                            row.granted_by.get_full_name() or row.granted_by.username
                        ) if row.granted_by else None,
                        "granted_at": row.updated_at,
                    }
                    for row in subject.capability_overrides.select_related("granted_by")
                ],
                "editable_by_me": allowed,
                "why_not": "" if allowed else why,
            })

        allowed, why = may_set_capabilities(request.user, subject)
        if not allowed:
            return Response({"detail": why}, status=status.HTTP_403_FORBIDDEN)

        ser = CapabilitySetSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        rows = ser.validated_data["overrides"]

        # Nobody may hand out what they do not hold themselves: that is how
        # a scoped lead would quietly become an unscoped one.
        mine = request.user.capabilities
        if request.user.role not in ("super_admin", "group_head"):
            overreach = sorted({r["capability"] for r in rows if r["allowed"]} - mine)
            if overreach:
                return Response(
                    {"detail": f"You cannot grant what you do not have yourself: {', '.join(overreach)}."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        from .capabilities import defaults_for

        defaults = defaults_for(subject.role)
        with transaction.atomic():
            subject.capability_overrides.all().delete()
            UserCapability.objects.bulk_create([
                UserCapability(
                    user=subject,
                    capability=r["capability"],
                    allowed=r["allowed"],
                    reason=r.get("reason", ""),
                    granted_by=request.user,
                )
                for r in rows
                # A row that agrees with the role is not an adjustment.
                if r["allowed"] != (r["capability"] in defaults)
            ])

        subject.refresh_from_db()
        return Response({
            "effective": sorted(subject.capabilities),
            "overrides": [
                {"capability": r.capability, "allowed": r.allowed, "reason": r.reason}
                for r in subject.capability_overrides.all()
            ],
        })

    def get_permissions(self):
        if self.action in ("create", "destroy"):
            return [IsSuperAdmin()]
        if self.action == "reset_password":
            return [IsSuperAdmin()]
        return super().get_permissions()

    @action(detail=False, methods=["get"])
    def me(self, request):
        serializer = UserSerializer(request.user, context=self.get_serializer_context())
        return Response(serializer.data)

    @action(detail=False, methods=["post"], url_path="change-password")
    def change_password(self, request):
        old_password = request.data.get("old_password")
        new_password = request.data.get("new_password")
        if not old_password or not new_password:
            return Response({"detail": "Both old_password and new_password are required."}, status=status.HTTP_400_BAD_REQUEST)
        if len(new_password) < 8:
            return Response({"detail": "Password must be at least 8 characters."}, status=status.HTTP_400_BAD_REQUEST)
        if not request.user.check_password(old_password):
            return Response({"detail": "Current password is incorrect."}, status=status.HTTP_400_BAD_REQUEST)
        request.user.set_password(new_password)
        request.user.save()
        return Response({"detail": "Password changed successfully."})

    @action(detail=True, methods=["post"], url_path="reset-password")
    def reset_password(self, request, pk=None):
        user = self.get_object()
        new_password = request.data.get("new_password")
        if not new_password or len(new_password) < 8:
            return Response({"detail": "Password must be at least 8 characters."}, status=status.HTTP_400_BAD_REQUEST)
        user.set_password(new_password)
        user.save()
        return Response({"detail": f"Password reset for {user.username}."})


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AuditLog.objects.select_related("user").all()
    serializer_class = AuditLogSerializer
    permission_classes = [IsSuperAdmin]
    filterset_fields = ["action", "resource_type", "user"]
    search_fields = ["resource_type", "detail"]
    ordering_fields = ["created_at"]


class CapabilityCatalogueView(APIView):
    """Every capability the system knows about, with each role's defaults."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .capabilities import MODULES, ROLE_DEFAULTS, catalogue

        return Response({
            "capabilities": catalogue(),
            "modules": list(MODULES),
            "role_defaults": {role: sorted(keys) for role, keys in ROLE_DEFAULTS.items()},
        })


def may_set_capabilities(actor, subject) -> tuple[bool, str]:
    """Who may change what somebody else is allowed to do.

    The capability itself is the gate, plus one rule that is not about
    rights at all: nobody edits their own. A person who can widen their own
    authority has no authority worth recording.
    """
    if actor.pk == subject.pk:
        return False, "Nobody changes their own permissions — ask someone above you."
    if not actor.can("manage_permissions"):
        return False, "You cannot change what other people may do."
    # Above that gate, a team lead is scoped to their own team; the roles
    # that run the company are not.
    if actor.role in ("super_admin", "group_head"):
        return True, ""
    if actor.manages(subject):
        return True, ""
    return False, "You can only change permissions for people who report to you."
