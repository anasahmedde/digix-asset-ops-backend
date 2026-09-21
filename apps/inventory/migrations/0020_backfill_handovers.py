"""Requests issued before hand-overs were recorded get one hand-over for what
went out, so the slip and the log read the same for old and new."""
from django.db import migrations


def backfill(apps, schema_editor):
    IssuanceRequest = apps.get_model("inventory", "IssuanceRequest")
    for req in IssuanceRequest.objects.filter(quantity_issued__gt=0, handovers=[]).select_related("issued_by"):
        # Historical models carry fields only, so the name is built by hand.
        user = req.issued_by
        issued_by = (f"{user.first_name} {user.last_name}".strip() or user.username) if user else ""
        at = req.last_issued_at or req.updated_at
        req.handovers = [{
            "at": at.isoformat() if at else "",
            "quantity": req.quantity_issued,
            "received_by": req.received_by or "",
            "issued_by": issued_by,
            "serials": list(req.issued_serials or []),
            "note": "Recorded before hand-overs were kept one by one.",
        }]
        req.save(update_fields=["handovers"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0019_issuancerequest_handovers")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
