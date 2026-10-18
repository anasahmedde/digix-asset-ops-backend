from django.contrib.auth import get_user_model
from django.db import transaction
from common.noops import RefusesSilentNoOps
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import SAFE_METHODS, BasePermission, IsAuthenticated
from rest_framework.exceptions import PermissionDenied, ValidationError
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.views import TokenObtainPairView

from common.permissions import ADMIN_ROLES, CapabilityGate, IsSuperAdmin

from .models import AuditLog, RoleDefinition, UserCapability
from .serializers import (
    AuditLogSerializer,
    CapabilitySetSerializer,
    RoleDefinitionSerializer,
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
        # A phone keyboard capitalises the first letter and a suggestion
        # adds a space after the word, so "Sharjeel " is how "sharjeel"
        # often arrives. Usernames are matched without either.
        username = str(request.data.get("username", "") or "").strip()
        password = request.data.get("password", "")

        if not username or not password:
            return Response(
                {"detail": "Username and password are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = User.objects.filter(username=username).first()
        if user is None:
            same = list(User.objects.filter(username__iexact=username)[:2])
            # Only when the spelling picks out one account: two that differ
            # only by case are never guessed between.
            user = same[0] if len(same) == 1 else None
        if user is None:
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

        # Signing in on the phone needs "Use the field app".
        from common.field_app import REFUSAL, from_field_app, may_use_field_app

        if from_field_app(request) and not may_use_field_app(user):
            return Response({"detail": REFUSAL}, status=status.HTTP_403_FORBIDDEN)

        try:
            # The account's own spelling, whatever case it was typed in.
            serializer = self.get_serializer(data={User.USERNAME_FIELD: user.get_username(), "password": password})
            serializer.is_valid(raise_exception=True)
            return Response(serializer.validated_data, status=status.HTTP_200_OK)
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
        # Managing people is a capability now, so whoever holds it may do
        # it — not only the one role that used to be hardcoded here.
        can = getattr(user, "can", None)
        if callable(can) and can("manage_team"):
            return True
        # Setting somebody's capabilities has its own, finer gate: a team
        # lead may do it for their own team, which this rule cannot express.
        if getattr(view, "action", None) == "capabilities":
            return True
        return obj.pk == user.pk


def may_remove(actor, subject) -> tuple[bool, str]:
    """Two things a Super Admin still may not do when removing an account.

    Creating and removing logins is the Super Admin's (see get_permissions),
    and that gate stays. Inside it, nothing said whom: an admin could delete
    their own login, or another superuser's. Neither is about rights - the
    first locks the door from inside, the second is not one admin's call.
    """
    if actor.pk == subject.pk:
        return False, "You cannot delete your own account."
    if subject.is_superuser and not actor.is_superuser:
        return False, "Only a superuser can delete a superuser."
    return True, ""


class UserViewSet(RefusesSilentNoOps, viewsets.ModelViewSet):
    queryset = User.objects.all()
    # Seeing who works here is a capability. An external client portal
    # login does not hold it, and used to read the whole directory.
    read_capability = "view_team"
    # Names only, for pickers: the storekeeper names who collects stock
    # without needing the whole directory.
    action_capabilities = {"names": ("view_team", "issue_stock")}
    permission_classes = [IsAuthenticated, IsSelfOrSuperAdmin, CapabilityGate]
    filterset_fields = ["role", "is_active", "is_field_staff"]

    @action(detail=False, methods=["get"])
    def names(self, request):
        """Everyone on the team who can collect: name and role, nothing more.

        ``?request=<material request>``: only the team of the manager of the
        project it is for - the manager and everyone under them - since that
        is who works on it. A request with no project, or a project with no
        manager, keeps the whole list.
        """
        from .models import RoleDefinition

        # Custom roles have their own names, as the Teams page shows them.
        labels = dict(RoleDefinition.objects.values_list("key", "label"))
        people = (
            User.objects.filter(is_active=True)
            .exclude(role__in=[User.Role.CLIENT_VIEWER, User.Role.VENDOR, User.Role.SUPER_ADMIN])
            .exclude(is_superuser=True)
            .order_by("first_name", "last_name", "username")
        )
        lead = self._project_manager_of_request(request.query_params.get("request"))
        if lead is not None:
            boss_of = dict(User.objects.values_list("pk", "reports_to_id"))

            def on_team(uid):
                seen, boss = set(), uid
                while boss is not None and boss not in seen:
                    if boss == lead.pk:
                        return True
                    seen.add(boss)
                    boss = boss_of.get(boss)
                return False

            people = sorted((u for u in people if on_team(u.pk)), key=lambda u: u.pk != lead.pk)
        return Response([
            {"id": str(u.pk), "name": u.get_full_name().strip() or u.username,
             "role": labels.get(u.role) or u.role.replace("_", " ").title()}
            for u in people
        ])

    @staticmethod
    def _project_manager_of_request(request_id):
        """The manager of the project a material request is for, if any."""
        if not request_id:
            return None
        from apps.inventory.models import IssuanceRequest

        try:
            ir = IssuanceRequest.objects.filter(pk=request_id).first()
        except (ValueError, DjangoValidationError):
            return None
        if ir is None:
            return None
        project = ir.project if ir.project_id else None
        if project is None:
            device = (ir.asset_component.device if ir.asset_component_id
                      else ir.maintenance_schedule.device if ir.maintenance_schedule_id else None)
            project = device.project_on if device is not None else None
        return project.manager if project is not None and project.manager_id else None

    def perform_destroy(self, instance):
        """Remove an account - once nobody is left hanging from it.

        Deleting takes the person's own records with them (attendance, chat,
        project memberships); what they did to other things stays, with the
        name gone. Their reports would be left pointing at nobody, so those
        move first - the chart is the place to drag them.
        """
        from rest_framework.exceptions import PermissionDenied
        from rest_framework.exceptions import ValidationError as _VE

        allowed, why = may_remove(self.request.user, instance)
        if not allowed:
            raise PermissionDenied(why)
        reports = instance.direct_reports.count()
        if reports:
            who = instance.get_full_name() or instance.username
            raise _VE({"detail": (
                f"{who} has {reports} direct report{'s' if reports != 1 else ''}. "
                "Move them to another manager first - drag them on the chart."
            )})
        instance.delete()
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
            from .roles import defaults_for

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
        if not request.user.can("act_across_teams"):
            overreach = sorted({r["capability"] for r in rows if r["allowed"]} - mine)
            if overreach:
                return Response(
                    {"detail": f"You cannot grant what you do not have yourself: {', '.join(overreach)}."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        from .capabilities import SUPER_ADMIN_ONLY
        from .roles import defaults_for

        if subject.role != "super_admin":
            locked = sorted({r["capability"] for r in rows if r["allowed"]} & SUPER_ADMIN_ONLY)
            if locked:
                return Response(
                    {"detail": f"Only the Administrator may hold: {', '.join(locked)}."},
                    status=status.HTTP_403_FORBIDDEN,
                )

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

    permission_classes = [IsAuthenticated, CapabilityGate]
    read_capability = "view_team"

    def get(self, request):
        from .capabilities import MODULES, catalogue
        from .roles import capability_map, ensure_seeded

        ensure_seeded()
        return Response({
            "capabilities": catalogue(),
            "modules": list(MODULES),
            "role_defaults": {role: sorted(keys) for role, keys in capability_map().items()},
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
    # Above that gate, a team lead is scoped to their own team; whoever acts
    # across teams is not.
    if actor.is_superuser or actor.can("act_across_teams"):
        return True, ""
    if actor.manages(subject):
        return True, ""
    return False, "You can only change permissions for people who report to you."


class ManagesPermissions(BasePermission):
    """Reading roles is open; changing them needs the capability."""

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        can = getattr(request.user, "can", None)
        return bool(callable(can) and can("manage_permissions"))


class RoleDefinitionViewSet(viewsets.ModelViewSet):
    """The roles themselves: what each one allows, and new ones.

    Everyone may read them — knowing what a role means is how you pick the
    right one — and ``manage_permissions`` is needed to change them.
    """

    serializer_class = RoleDefinitionSerializer
    permission_classes = [IsAuthenticated, ManagesPermissions, CapabilityGate]
    # The permission model itself is internal: it says who may approve
    # spending and who may administer the system. An external viewer read
    # the whole matrix, headcounts included.
    read_capability = "view_team"

    def get_queryset(self):
        from .roles import ensure_seeded

        ensure_seeded()
        return RoleDefinition.objects.all()

    def perform_create(self, serializer):
        from django.utils.text import slugify

        label = serializer.validated_data["label"]
        key = serializer.validated_data.get("key") or slugify(label).replace("-", "_")[:50]
        if RoleDefinition.objects.filter(key=key).exists():
            raise ValidationError({"key": [f"A role keyed '{key}' already exists."]})
        serializer.save(key=key, is_builtin=False, created_by=self.request.user)

    def perform_update(self, serializer):
        role = serializer.instance
        # Nobody hands out through a role what they could not hand out
        # directly — the same rule as setting one person's capabilities.
        actor = self.request.user
        if not (actor.is_superuser or actor.can("act_across_teams")):
            wanted = set(serializer.validated_data.get("capabilities", role.capabilities))
            overreach = sorted(wanted - set(role.capabilities) - actor.capabilities)
            if overreach:
                raise PermissionDenied(
                    f"You cannot grant what you do not have yourself: {', '.join(overreach)}."
                )
        serializer.save()

    def perform_destroy(self, instance):
        if instance.is_builtin:
            raise ValidationError({
                "detail": [
                    "A built-in role cannot be deleted — accounts are stored against it. "
                    "Switch it off instead, or empty its capabilities."
                ]
            })
        holders = instance.holders
        if holders:
            raise ValidationError({
                "detail": [
                    f"{holders} person(s) still hold this role. Move them to another one first."
                ]
            })
        instance.delete()


class MenuOrderView(APIView):
    """The order of the sidebar's modules, the same for everyone.

    Everybody reads it; changing it is configuring the system (manage_setup).
    An empty list puts the menu back the way it ships.
    """

    permission_classes = [IsAuthenticated]
    KEY = "menu_order"

    def get(self, request):
        from .models import AppSetting

        row = AppSetting.objects.filter(key=self.KEY).first()
        return Response({"order": (row.value or {}).get("order", []) if row else []})

    def put(self, request):
        from .models import AppSetting

        if not (request.user.is_superuser or request.user.can("manage_setup")):
            raise PermissionDenied("Only the Administrator arranges the menu.")
        order = request.data.get("order")
        if not isinstance(order, list) or not all(isinstance(h, str) and h.startswith("/") for h in order):
            raise ValidationError({"order": ["A list of page addresses, top to bottom."]})
        order = list(dict.fromkeys(order))[:100]
        AppSetting.objects.update_or_create(key=self.KEY, defaults={"value": {"order": order}})
        return Response({"order": order})
