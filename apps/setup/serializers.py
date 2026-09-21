from rest_framework import serializers

from .models import (
    Company,
    EscalationPolicy,
    NumberingScheme,
    PaymentTerms,
    TermsTemplate,
    UnitOfMeasure,
    WarrantyPeriodPreset,
)


class CompanySerializer(serializers.ModelSerializer):
    class Meta:
        model = Company
        fields = [
            "id", "name", "legal_name", "logo", "address", "city",
            "state_province", "country", "phone", "email", "website",
            "tax_id", "registration_number", "default_currency", "is_primary",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class NumberingSchemeSerializer(serializers.ModelSerializer):
    entity_display = serializers.CharField(source="get_entity_display", read_only=True)
    preview = serializers.CharField(read_only=True)

    class Meta:
        model = NumberingScheme
        fields = [
            "id", "entity", "entity_display", "prefix", "separator",
            "include_year", "padding", "next_number", "is_active",
            "preview", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "entity_display", "preview", "created_at", "updated_at"]


class PaymentTermsSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentTerms
        fields = [
            "id", "name", "code", "days", "description", "is_active",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class UnitOfMeasureSerializer(serializers.ModelSerializer):
    # How many components count in this unit — shown so nobody deletes a live one.
    in_use = serializers.SerializerMethodField()

    class Meta:
        model = UnitOfMeasure
        fields = ["id", "name", "symbol", "description", "is_active", "in_use", "created_at", "updated_at"]
        read_only_fields = ["id", "in_use", "created_at", "updated_at"]

    def get_in_use(self, obj):
        return obj.usage_count()

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Give the unit a name.")
        clash = UnitOfMeasure.objects.filter(name__iexact=value)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(f"'{value}' is already on the list.")
        return value


class TermsTemplateSerializer(serializers.ModelSerializer):
    category_display = serializers.CharField(source="get_category_display", read_only=True)

    class Meta:
        model = TermsTemplate
        fields = [
            "id", "name", "category", "category_display", "body",
            "is_default", "is_active", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "category_display", "created_at", "updated_at"]


class WarrantyPeriodPresetSerializer(serializers.ModelSerializer):
    class Meta:
        model = WarrantyPeriodPreset
        fields = ["id", "label", "months", "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]


class EscalationPolicySerializer(serializers.ModelSerializer):
    trigger_display = serializers.CharField(source="get_trigger_display", read_only=True)
    scope_display = serializers.CharField(source="get_scope_display", read_only=True)
    stage = serializers.IntegerField(min_value=1, max_value=3, default=1)

    class Meta:
        model = EscalationPolicy
        fields = [
            "id", "scope", "scope_display", "trigger", "trigger_display", "stage",
            "hours", "escalate_to_role", "also_notify_role", "is_active",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "scope_display", "trigger_display", "created_at", "updated_at"]
