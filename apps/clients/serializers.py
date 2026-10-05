from rest_framework import serializers

from common.contacts import WritesContacts

from .models import Client, ClientContact


class ClientContactSerializer(serializers.ModelSerializer):
    """Somebody to ring at this client, and the job they do."""

    class Meta:
        model = ClientContact
        fields = [
            "id", "client", "name", "designation", "phone", "email",
            "is_primary", "notes", "created_at",
        ]
        read_only_fields = ["id", "client", "created_at"]


class ClientSerializer(WritesContacts, serializers.ModelSerializer):
    contacts = ClientContactSerializer(many=True, required=False)

    class Meta:
        model = Client
        fields = [
            "id", "name", "code", "contact_person", "contact_email",
            "contact_phone", "address", "contacts",
            "is_active", "created_at", "updated_at",
        ]
        # The code is the register's to issue, and the three contact_*
        # fields are written from whichever contact is primary.
        read_only_fields = [
            "id", "code", "contact_person", "contact_email", "contact_phone",
            "created_at", "updated_at",
        ]

    def validate(self, attrs):
        """A client is a name, an address, and somebody to ring.

        All three are asked for on the way in rather than chased later: a
        client with no number on file is one somebody has to go and find
        out about before anything can be done.
        """
        def current(name):
            return attrs[name] if name in attrs else getattr(self.instance, name, None)

        missing = {}
        if not (current("name") or "").strip():
            missing["name"] = "Say who the client is."
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
