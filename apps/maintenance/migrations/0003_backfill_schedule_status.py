from django.db import migrations


def backfill(apps, schema_editor):
    MaintenanceSchedule = apps.get_model("maintenance", "MaintenanceSchedule")
    # Inactive schedules become "on_hold"; active ones keep the default "active".
    MaintenanceSchedule.objects.filter(is_active=False).update(status="on_hold")


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("maintenance", "0002_maintenanceschedule_status")]
    operations = [migrations.RunPython(backfill, noop)]
