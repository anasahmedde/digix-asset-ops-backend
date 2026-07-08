from rest_framework import serializers

from .models import Supplier, SupplierContact, SupplierServiceCategory


class SupplierServiceCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = SupplierServiceCategory
        fields = ["id", "name", "description", "is_active", "created_at"]
        read_only_fields = ["id", "created_at"]


class SupplierContactSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupplierContact
        fields = [
            "id", "supplier", "name", "designation", "phone", "email",
            "is_primary", "notes", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class SupplierSerializer(serializers.ModelSerializer):
    contacts = SupplierContactSerializer(many=True, read_only=True)
    service_category_names = serializers.SerializerMethodField()

    class Meta:
        model = Supplier
        fields = [
            "id", "name", "code", "service_categories", "service_category_names",
            "contact_person", "contact_email", "contact_phone", "address",
            "website", "is_active", "contacts", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "code", "created_at", "updated_at"]

    def get_service_category_names(self, obj):
        return [c.name for c in obj.service_categories.all()]
