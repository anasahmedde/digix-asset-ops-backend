"""An asset is identified by its asset code, not by an invented serial.

Every asset created before this carries its own asset code in the serial
number field, because the field was unique and could not hold two blanks. That
serial was never a real one — no manufacturer issued it — and it printed on
purchase orders as "S/N DGX-2026-00101" beside the same code. The field becomes
nullable and those copies are cleared, leaving a serial only where somebody
actually typed a different one.
"""
from django.db import migrations, models
from django.db.models import F


def clear_the_serials_that_were_only_the_asset_code(apps, schema_editor):
    Device = apps.get_model("assets", "Device")
    Device.objects.filter(serial_number=F("asset_code")).update(serial_number=None)
    Device.objects.filter(serial_number="").update(serial_number=None)


def put_the_asset_code_back(apps, schema_editor):
    Device = apps.get_model("assets", "Device")
    Device.objects.filter(serial_number__isnull=True).update(serial_number=F("asset_code"))


class Migration(migrations.Migration):

    dependencies = [("assets", "0029_device_installation_cost")]

    operations = [
        migrations.AlterField(
            model_name="device",
            name="serial_number",
            field=models.CharField(
                blank=True, default=None,
                help_text="The manufacturer's serial, where there is one. Assets are identified by their asset code.",
                max_length=200, null=True, unique=True,
            ),
        ),
        migrations.RunPython(
            clear_the_serials_that_were_only_the_asset_code, put_the_asset_code_back,
        ),
    ]
