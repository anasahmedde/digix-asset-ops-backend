"""Backfill the new devices M2M from the primary device FK (MW-03)."""

from django.db import migrations


def backfill_devices(apps, schema_editor):
    Ticket = apps.get_model("tickets", "Ticket")
    Through = Ticket.devices.through
    rows = [
        Through(ticket_id=ticket_id, device_id=device_id)
        for ticket_id, device_id in Ticket.objects.filter(
            device__isnull=False
        ).values_list("id", "device_id")
    ]
    Through.objects.bulk_create(rows, ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [
        ("tickets", "0013_ticket_charge_to_ticket_devices_ticket_is_billable_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_devices, migrations.RunPython.noop),
    ]
