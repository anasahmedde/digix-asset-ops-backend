from rest_framework import serializers

from .models import (
    Company,
    NumberingScheme,
    PaymentTerms,
    TermsTemplate,
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
