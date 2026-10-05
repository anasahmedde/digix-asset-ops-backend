"""People to ring at an organisation, written through its own record.

A client and a supplier each used to carry one name and one number, which
is never how an organisation works — the person who signs the order is
rarely the person who opens the gate. The contacts are a list now, and
this is the one place that knows how to write it, so a client and a
supplier behave identically rather than nearly.

The organisation's own ``contact_*`` fields are kept in step with whichever
contact is marked primary, so every screen, export and PDF already reading
them carries on working and shows the same person the list calls primary.
"""
from rest_framework import serializers


class WritesContacts:
    """Nested, writable ``contacts`` for a model with a contacts relation.

    Mix in ahead of ``ModelSerializer``. The host declares its own nested
    serializer; this handles saving it and keeping the primary mirrored.
    """

    #: The organisation is somebody's to ring: a name and a number at least.
    REQUIRED_ON_CONTACT = ("name", "phone")

    def validate_contacts(self, rows):
        for i, row in enumerate(rows or [], start=1):
            missing = [f for f in self.REQUIRED_ON_CONTACT if not (row.get(f) or "").strip()]
            if missing:
                raise serializers.ValidationError(
                    f"Contact {i} needs a {' and a '.join(missing)}."
                )
        names = [(r.get("name") or "").strip().lower() for r in rows or []]
        if len(set(names)) != len(names):
            raise serializers.ValidationError("The same person is listed twice.")
        if sum(1 for r in rows or [] if r.get("is_primary")) > 1:
            raise serializers.ValidationError("Only one contact is the primary one.")
        return rows

    def _save_contacts(self, instance, rows):
        """Replace the list, then mirror the primary onto the record."""
        if rows is None:
            return
        related = instance.contacts
        related.all().delete()
        made = [
            related.create(
                name=(r.get("name") or "").strip(),
                designation=(r.get("designation") or "").strip(),
                phone=(r.get("phone") or "").strip(),
                email=(r.get("email") or "").strip(),
                is_primary=bool(r.get("is_primary")),
                notes=(r.get("notes") or "").strip(),
            )
            for r in rows
        ]
        # Nobody said which is primary, so the first one is: a list of
        # people with no primary leaves every other screen showing blank.
        primary = next((c for c in made if c.is_primary), made[0] if made else None)
        if primary is not None and not primary.is_primary:
            primary.is_primary = True
            primary.save(update_fields=["is_primary"])
        instance.contact_person = primary.name if primary else ""
        instance.contact_phone = primary.phone if primary else ""
        instance.contact_email = primary.email if primary else ""
        instance.save(update_fields=[
            "contact_person", "contact_phone", "contact_email", "updated_at",
        ])

    def create(self, validated_data):
        rows = validated_data.pop("contacts", None)
        instance = super().create(validated_data)
        self._save_contacts(instance, rows)
        return instance

    def update(self, instance, validated_data):
        rows = validated_data.pop("contacts", None)
        instance = super().update(instance, validated_data)
        self._save_contacts(instance, rows)
        return instance
