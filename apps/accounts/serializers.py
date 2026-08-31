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

    # The only fields a non-super_admin may write (on their own record).
    SELF_WRITABLE_FIELDS = ("first_name", "last_name", "email", "phone", "avatar")

    class Meta:
        model = User
        fields = [
            "id", "username", "email", "first_name", "last_name",
            "full_name", "role", "job_title", "phone", "avatar", "is_field_staff",
            "employee_id", "cnic", "join_date", "leaving_date",
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
