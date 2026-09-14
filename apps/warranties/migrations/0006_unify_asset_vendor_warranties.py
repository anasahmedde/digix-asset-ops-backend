from django.db import migrations


def unify(apps, schema_editor):
    """Asset-level cover from outside is the vendor's warranty.

    Older rows on the asset itself were typed Manufacturer or Extended. They
    become Vendor, and each keeps a note of what it was recorded as, so the
    change is traceable. Component warranties are left untouched — a part's
    own manufacturer cover stays a manufacturer warranty.
    """
    Warranty = apps.get_model("warranties", "Warranty")
    labels = {"manufacturer": "Manufacturer", "extended": "Extended"}
    for warranty in Warranty.objects.filter(component__isnull=True, warranty_type__in=list(labels)):
        note = f"Recorded as {labels[warranty.warranty_type]} before asset-level cover was unified as the vendor warranty."
        warranty.notes = f"{warranty.notes}\n{note}" if warranty.notes else note
        warranty.warranty_type = "supplier"
        warranty.save(update_fields=["warranty_type", "notes"])


class Migration(migrations.Migration):
    dependencies = [("warranties", "0005_alter_warranty_warranty_type")]

    operations = [migrations.RunPython(unify, migrations.RunPython.noop)]
