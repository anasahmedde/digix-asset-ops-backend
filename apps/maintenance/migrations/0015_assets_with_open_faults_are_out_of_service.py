from django.db import migrations

OPEN_STATUSES = ("active", "pending", "in_process", "on_hold", "overdue")


def take_them_out_of_service(apps, schema_editor):
    """An asset with a fault still open against it is not in service.

    Jobs raised before the registry was kept in step left assets reading
    Active while the maintenance register had work open on them.
    """
    Device = apps.get_model("assets", "Device")
    Schedule = apps.get_model("maintenance", "MaintenanceSchedule")

    faulty = Schedule.objects.filter(
        maintenance_type="corrective", status__in=OPEN_STATUSES, device__isnull=False,
    ).values_list("device_id", flat=True)
    Device.objects.filter(pk__in=list(faulty), status__in=("active", "installed")).update(
        status="under_maintenance"
    )


def leave_them(apps, schema_editor):
    """Nothing to undo: the status each asset held before is not recorded."""


class Migration(migrations.Migration):

    dependencies = [
        ("maintenance", "0014_jobs_for_open_tickets"),
        ("assets", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(take_them_out_of_service, leave_them),
    ]
