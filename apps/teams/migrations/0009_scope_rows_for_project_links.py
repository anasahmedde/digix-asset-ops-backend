from django.db import migrations


def backfill(apps, schema_editor):
    """Give every asset linked through its own Project field a Scope line.

    Before the two were kept in sync, an asset linked from the asset form was
    costed on the project but never appeared in its Scope list.
    """
    Device = apps.get_model("assets", "Device")
    ScopeItem = apps.get_model("teams", "ProjectScopeItem")
    for device in Device.objects.filter(project__isnull=False):
        if ScopeItem.objects.filter(project_id=device.project_id, device=device).exists():
            continue
        ScopeItem.objects.create(
            project_id=device.project_id,
            device=device,
            quantity=1,
            site_id=device.current_site_id,
            notes="Linked from the asset's Project field",
        )


class Migration(migrations.Migration):
    dependencies = [
        ("teams", "0008_projectbudget_projectcostline"),
        ("assets", "0020_device_supply_vendor_contact_and_more"),
    ]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
