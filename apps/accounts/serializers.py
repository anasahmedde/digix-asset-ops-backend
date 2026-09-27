from django.contrib.auth import get_user_model
from rest_framework import serializers

from common.permissions import ADMIN_ROLES

from .models import AuditLog

User = get_user_model()


def _is_super_admin(user):
    return bool(
        user
        and user.is_authenticated
        and (user.is_superuser or getattr(user, "role", None) in ADMIN_ROLES)
    )


class UserSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()
    supplier_name = serializers.CharField(source="supplier.name", read_only=True)
    reports_to_name = serializers.SerializerMethodField()
    direct_report_count = serializers.SerializerMethodField()

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
            "full_name", "role", "job_title", "phone", "avatar", "is_field_staff",
            "employee_id", "cnic", "join_date", "leaving_date",
            "reports_to", "reports_to_name", "direct_report_count", "capabilities",
            "supplier", "supplier_name",
            "is_active", "date_joined",
        ]
        read_only_fields = ["id", "date_joined"]

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        if not _is_super_admin(getattr(request, "user", None)):
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


class UserCreateSerializer(serializers.ModelSerializer):
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
