from rest_framework import serializers

from common.contacts import WritesContacts

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
        read_only_fields = ["id", "supplier", "created_at"]


class SupplierSerializer(WritesContacts, serializers.ModelSerializer):
    contacts = SupplierContactSerializer(many=True, required=False)
    service_category_names = serializers.SerializerMethodField()

    class Meta:
        model = Supplier
        fields = [
            "id", "name", "code", "service_categories", "service_category_names",
            "contact_person", "contact_email", "contact_phone", "address",
            "website", "is_active", "contacts", "created_at", "updated_at",
        ]
        # The code is the register's to issue, and the three contact_*
        # fields are written from whichever contact is primary.
        read_only_fields = [
            "id", "code", "contact_person", "contact_email", "contact_phone",
            "created_at", "updated_at",
        ]

    def validate(self, attrs):
        """A supplier is a name, an address, and somebody to ring."""
        def current(name):
            return attrs[name] if name in attrs else getattr(self.instance, name, None)

        missing = {}
        if not (current("name") or "").strip():
            missing["name"] = "Say who the supplier is."
        if not (current("address") or "").strip():
            missing["address"] = "Say where they are."
        contacts = attrs.get("contacts")
        if contacts is None and self.instance is not None:
            contacts = list(self.instance.contacts.all())
        if not contacts:
            missing["contacts"] = "Add at least one contact — a name and a number."
        if missing:
            raise serializers.ValidationError(missing)
        return attrs

    def get_service_category_names(self, obj):
        return [c.name for c in obj.service_categories.all()]
