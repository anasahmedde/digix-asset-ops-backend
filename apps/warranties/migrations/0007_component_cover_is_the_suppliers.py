"""A component's warranty comes from the supplier it was received from: one
kind. Rows typed as manufacturer or extended by the old forms are corrected."""
from django.db import migrations


def one_kind(apps, schema_editor):
    Warranty = apps.get_model("warranties", "Warranty")
    Warranty.objects.filter(component__isnull=False).exclude(warranty_type="supplier").update(warranty_type="supplier")
    InventoryUnit = apps.get_model("inventory", "InventoryUnit")
    InventoryUnit.objects.filter(has_warranty=True).exclude(warranty_type="supplier").update(warranty_type="supplier")


class Migration(migrations.Migration):
    dependencies = [
        ("warranties", "0006_unify_asset_vendor_warranties"),
        ("inventory", "0014_issuancerequest_awaiting_procurement"),
    ]
    operations = [migrations.RunPython(one_kind, migrations.RunPython.noop)]
