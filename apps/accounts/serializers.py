from django.contrib.auth import get_user_model
from rest_framework import serializers

from common.permissions import ADMIN_ROLES

from .models import AuditLog, RoleDefinition
from common.dates import DateOrder

User = get_user_model()


def _is_super_admin(user):
    return bool(
        user
        and user.is_authenticated
        and (user.is_superuser or getattr(user, "role", None) in ADMIN_ROLES)
    )


class UserSerializer(DateOrder, serializers.ModelSerializer):
    date_order = (("leaving_date", "join_date", "the joining date"),)
    full_name = serializers.SerializerMethodField()
    supplier_name = serializers.CharField(source="supplier.name", read_only=True)
    reports_to_name = serializers.SerializerMethodField()
    direct_report_count = serializers.SerializerMethodField()
    # The role as the screen names it. Roles are records now, so a key the
    # frontend has never heard of (a custom role) still reads as a name.
    role_label = serializers.SerializerMethodField()

    def get_role_label(self, obj):
        labels = self.context.get("_role_labels")
        if labels is None:
            from .models import RoleDefinition
            labels = dict(RoleDefinition.objects.values_list("key", "label"))
            self.context["_role_labels"] = labels
        return labels.get(obj.role) or obj.role.replace("_", " ").title()

    def validate_reports_to(self, boss):
        """The chart is a tree, and moving somebody on it is a manager's call.

        Two things could go wrong and nothing stopped either: a person could
        be put under one of their own reports, which turns the line into a
        loop the chart cannot draw and `manages()` cannot walk; and anyone
        with the write rule could re-hang anyone, so a team lead could move
        the Operations Head under themselves.
        """
        if boss is None:
            return boss
        person = self.instance
        if person is not None:
            if boss.pk == person.pk:
                raise serializers.ValidationError("Nobody reports to themselves.")
            if person.manages(boss):
                who = boss.get_full_name() or boss.username
                raise serializers.ValidationError(
                    f"{who} reports to this person - that would make a loop."
                )
        request = self.context.get("request")
        actor = getattr(request, "user", None)
        if actor is not None and person is not None and not (
            actor.is_superuser
            or actor.can("act_across_teams")
            or actor.manages(person)
        ):
            raise serializers.ValidationError(
                "You can only move people who report to you."
            )
        return boss

    def get_reports_to_name(self, obj):
        boss = obj.reports_to
        if boss is None:
            return None
        return boss.get_full_name() or boss.username

    def get_direct_report_count(self, obj):
        return obj.direct_reports.count()

    # Everything this person may do, after their role's defaults are
    # adjusted. The screen hides what it must from this, and the server
    # still checks on every call.
    capabilities = serializers.SerializerMethodField()

    def get_capabilities(self, obj):
        return sorted(obj.capabilities)

    # The only fields a non-super_admin may write (on their own record).
    # `supplier` is deliberately NOT here: linking a login to a vendor is a
    # super_admin decision (it grants that supplier's portal scope).
    SELF_WRITABLE_FIELDS = ("first_name", "last_name", "email", "phone", "avatar")

    class Meta:
        model = User
        fields = [
            "id", "username", "email", "first_name", "last_name",
            "full_name", "role", "role_label", "job_title", "phone", "avatar", "is_field_staff",
            "employee_id", "cnic", "join_date", "leaving_date",
            "reports_to", "reports_to_name", "direct_report_count", "capabilities",
            "supplier", "supplier_name",
            "is_active", "date_joined",
        ]
        read_only_fields = ["id", "date_joined"]

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        actor = getattr(request, "user", None)
        # Managing people is a capability; whoever holds it writes the whole
        # record, not just their own profile fields.
        can = getattr(actor, "can", None)
        may_manage = _is_super_admin(actor) or (callable(can) and can("manage_team"))

        # Nobody rewrites their own authority. The rule was enforced on the
        # capabilities endpoint but not here, so a super_admin could PATCH
        # his own `role` and change what he may do through the side door.
        editing_self = (
            self.instance is not None
            and getattr(actor, "pk", None) == getattr(self.instance, "pk", None)
        )
        if editing_self:
            # Nobody picks their own manager, either.
            for name in ("role", "is_active", "is_staff", "is_superuser", "reports_to"):
                if name in fields:
                    fields[name].read_only = True

        if not may_manage:
            # Non-admins can only edit safe profile fields; everything else
            # (role, is_active, HR fields, username, ...) becomes read-only.
            for name, field in fields.items():
                if name not in self.SELF_WRITABLE_FIELDS:
                    field.read_only = True
        return fields

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        user = getattr(request, "user", None)
        is_own_record = bool(
            user and user.is_authenticated and user.pk == instance.pk
        )
        if not (_is_super_admin(user) or is_own_record):
            # CNIC is national-ID PII: only super_admin or the user themselves
            # may read it.
            data["cnic"] = None
        return data

    def get_full_name(self, obj):
        return obj.get_full_name()


class UserCreateSerializer(DateOrder, serializers.ModelSerializer):
    date_order = (("leaving_date", "join_date", "the joining date"),)
    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = User
        fields = [
            "id", "username", "email", "password", "first_name",
            "last_name", "role", "job_title", "phone", "is_field_staff",
            "employee_id", "cnic", "join_date", "leaving_date",
            "supplier",
        ]
        read_only_fields = ["id"]

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user


class AuditLogSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source="user.get_full_name", read_only=True)

    class Meta:
        model = AuditLog
        fields = [
            "id", "user", "user_name", "action", "resource_type",
            "resource_id", "detail", "ip_address", "created_at",
        ]
        read_only_fields = fields


class CapabilityOverrideSerializer(serializers.Serializer):
    """One adjustment: this capability, granted or withdrawn, and why."""

    capability = serializers.CharField()
    allowed = serializers.BooleanField()
    reason = serializers.CharField(required=False, allow_blank=True, max_length=300)

    def validate_capability(self, value):
        from .capabilities import ALL_KEYS

        if value not in ALL_KEYS:
            raise serializers.ValidationError(f"There is no capability called '{value}'.")
        return value


class CapabilitySetSerializer(serializers.Serializer):
    """The whole set of adjustments for one person, replacing what was there."""

    overrides = CapabilityOverrideSerializer(many=True)


class RoleDefinitionSerializer(serializers.ModelSerializer):
    """A role and the capabilities it grants."""

    holders = serializers.IntegerField(read_only=True)
    created_by_name = serializers.SerializerMethodField()
    # Derived from the label when a role is written; fixed thereafter,
    # because every account stores it.
    key = serializers.SlugField(max_length=50, required=False)

    def get_created_by_name(self, obj):
        who = obj.created_by
        return (who.get_full_name() or who.username) if who else None

    class Meta:
        model = RoleDefinition
        fields = [
            "id", "key", "label", "description", "capabilities",
            "is_builtin", "is_active", "holders", "created_by_name",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "is_builtin", "created_at", "updated_at"]

    def validate_capabilities(self, value):
        from .capabilities import ALL_KEYS

        if not isinstance(value, list):
            raise serializers.ValidationError("Send a list of capability keys.")
        unknown = sorted(set(value) - ALL_KEYS)
        if unknown:
            raise serializers.ValidationError(f"No such capability: {', '.join(unknown)}.")
        return sorted(set(value))

    def validate_key(self, value):
        # The key is what every account stores, so it is fixed once set.
        if self.instance and value != self.instance.key:
            raise serializers.ValidationError(
                "A role's key cannot change — accounts are stored against it. "
                "Rename the label instead."
            )
        return value

    def validate_label(self, value):
        if not value.strip():
            raise serializers.ValidationError("Give the role a name.")
        return value.strip()
