"""Maintenance is preventive or corrective. Predictive was neither.

Nobody raised a predictive job: every schedule on the board is either planned
ahead or a response to a fault. Any that exist are planned work, so they become
preventive.
"""
from django.db import migrations, models


def predictive_was_planned_work(apps, schema_editor):
    MaintenanceSchedule = apps.get_model("maintenance", "MaintenanceSchedule")
    MaintenanceSchedule.objects.filter(maintenance_type="predictive").update(
        maintenance_type="preventive",
    )


def nothing_to_put_back(apps, schema_editor):
    """Reversible in schema only: which jobs were predictive is not recorded."""


class Migration(migrations.Migration):

    dependencies = [("maintenance", "0007_maintenancerecord_charge_to_and_more")]

    operations = [
        migrations.RunPython(predictive_was_planned_work, nothing_to_put_back),
        migrations.AlterField(
            model_name="maintenanceschedule",
            name="maintenance_type",
            field=models.CharField(
                choices=[("preventive", "Preventive"), ("corrective", "Corrective")],
                default="preventive", max_length=15,
            ),
        ),
    ]
