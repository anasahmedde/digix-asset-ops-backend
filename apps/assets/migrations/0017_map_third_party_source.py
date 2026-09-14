from django.db import migrations


def forwards(apps, schema_editor):
    """Old two-way source becomes the three-way delivery route.

    Everything previously marked "third_party" was bought in and installed by
    us, which is exactly the new ``vendor_supplied`` route. Nothing can be
    inferred as turnkey, so no row is guessed into it.
    """
    Device = apps.get_model("assets", "Device")
    Device.objects.filter(source="third_party").update(source="vendor_supplied")


def backwards(apps, schema_editor):
    Device = apps.get_model("assets", "Device")
    Device.objects.filter(source__in=["vendor_supplied", "vendor_turnkey"]).update(
        source="third_party"
    )


class Migration(migrations.Migration):
    dependencies = [("assets", "0016_alter_device_device_model_alter_device_source_and_more")]
    operations = [migrations.RunPython(forwards, backwards)]
