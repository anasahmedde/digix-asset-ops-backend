"""A part's cover is numbered from the component-warranty series; anything
typed into its reference so far was the vendor's number and moves beside it."""
from datetime import datetime

from django.db import migrations, models


def _next(Scheme, entity):
    scheme = Scheme.objects.get(entity=entity)
    parts = [scheme.prefix]
    if scheme.include_year:
        parts.append(str(datetime.now().year))
    parts.append(str(scheme.next_number).zfill(scheme.padding))
    scheme.next_number += 1
    scheme.save(update_fields=["next_number"])
    return scheme.separator.join(p for p in parts if p)


def number_them(apps, schema_editor):
    InventoryUnit = apps.get_model("inventory", "InventoryUnit")
    Scheme = apps.get_model("setup", "NumberingScheme")
    for unit in InventoryUnit.objects.filter(has_warranty=True).order_by("created_at"):
        unit.warranty_vendor_reference = (unit.warranty_reference or "").strip()
        unit.warranty_reference = _next(Scheme, "component_warranty")
        unit.save(update_fields=["warranty_reference", "warranty_vendor_reference"])


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0030_parts_carry_a_warranty_reference"),
        ("setup", "0019_seed_warranty_numbering"),
    ]

    operations = [
        migrations.AddField(
            model_name="inventoryunit",
            name="warranty_vendor_reference",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AlterField(
            model_name="inventoryunit",
            name="warranty_reference",
            field=models.CharField(blank=True, db_index=True, max_length=200),
        ),
        migrations.RunPython(number_them, migrations.RunPython.noop),
    ]
