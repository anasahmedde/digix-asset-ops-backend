from datetime import datetime

from django.db import migrations, models


def backfill(apps, schema_editor):
    """Assign TKT numbers to existing tickets, oldest first, using the active
    numbering scheme's format and advancing its counter."""
    Ticket = apps.get_model("tickets", "Ticket")
    Scheme = apps.get_model("setup", "NumberingScheme")

    scheme = Scheme.objects.filter(entity="ticket", is_active=True).first()

    def build(number: int) -> str:
        if scheme is None:
            return f"TKT-{datetime.now().year}-{str(number).zfill(5)}"
        segments = [scheme.prefix]
        if scheme.include_year:
            segments.append(str(datetime.now().year))
        segments.append(str(number).zfill(scheme.padding))
        return scheme.separator.join(s for s in segments if s)

    number = scheme.next_number if scheme else 1
    for ticket in Ticket.objects.order_by("created_at").iterator():
        ticket.ticket_number = build(number)
        ticket.save(update_fields=["ticket_number"])
        number += 1

    if scheme is not None:
        scheme.next_number = number
        scheme.save(update_fields=["next_number"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("tickets", "0003_alter_ticket_status"),
        ("setup", "0002_seed_numbering_schemes"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="ticket_number",
            field=models.CharField(blank=True, db_index=True, max_length=50),
        ),
        migrations.RunPython(backfill, noop),
        migrations.AlterField(
            model_name="ticket",
            name="ticket_number",
            field=models.CharField(blank=True, db_index=True, max_length=50, unique=True),
        ),
    ]
