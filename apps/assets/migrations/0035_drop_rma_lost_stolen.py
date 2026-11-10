# RMA and Lost/Stolen are no longer asset statuses. Any asset still on one
# moves to its nearest: an RMA unit is out for repair (Under Maintenance),
# a lost or stolen one is gone (Decommissioned).
from django.db import migrations, models


def move_off_dropped_statuses(apps, schema_editor):
    Device = apps.get_model("assets", "Device")
    Device.objects.filter(status="rma").update(status="under_maintenance")
    Device.objects.filter(status="lost_stolen").update(status="decommissioned")


class Migration(migrations.Migration):

    dependencies = [
        ('assets', '0034_leftovers_settled'),
    ]

    operations = [
        migrations.RunPython(move_off_dropped_statuses, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='device',
            name='status',
            field=models.CharField(choices=[('procured', 'In Procurement'), ('in_production', 'In Production'), ('in_stock', 'In Stock'), ('assigned', 'Assigned for Installation'), ('installed', 'Installed'), ('active', 'Active'), ('under_maintenance', 'Under Maintenance'), ('client_property', 'Client Property'), ('decommissioned', 'Decommissioned'), ('in_transit', 'In Transit')], default='procured', max_length=20),
        ),
    ]
