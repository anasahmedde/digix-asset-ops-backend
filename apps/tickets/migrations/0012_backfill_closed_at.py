from django.db import migrations
from django.db.models import F


def backfill_closed_at(apps, schema_editor):
    """Legacy closed tickets predate closed_at (added in 0011 without a
    backfill); stamp them with updated_at so the reopen window applies."""
    Ticket = apps.get_model("tickets", "Ticket")
    Ticket.objects.using(schema_editor.connection.alias).filter(
        status="closed", closed_at__isnull=True
    ).update(closed_at=F("updated_at"))


class Migration(migrations.Migration):

    dependencies = [
        ("tickets", "0011_ticket_closed_at"),
    ]

    operations = [
        migrations.RunPython(backfill_closed_at, migrations.RunPython.noop),
    ]
