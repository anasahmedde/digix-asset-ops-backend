"""The unit list starts with the usual units and every unit components already
count in, so nothing in use goes missing from the dropdown."""
from django.db import migrations

DEFAULTS = [
    ("piece", "pc"), ("meter", "m"), ("box", ""), ("roll", ""),
    ("kg", "kg"), ("litre", "L"), ("set", ""), ("pair", ""),
]


def seed(apps, schema_editor):
    UnitOfMeasure = apps.get_model("setup", "UnitOfMeasure")
    MaterialType = apps.get_model("assets", "MaterialType")
    InventoryUnitType = apps.get_model("inventory", "InventoryUnitType")
    seen = set()
    for name, symbol in DEFAULTS:
        UnitOfMeasure.objects.get_or_create(name=name, defaults={"symbol": symbol})
        seen.add(name.lower())
    in_use = set(MaterialType.objects.exclude(unit="").values_list("unit", flat=True))
    in_use |= set(InventoryUnitType.objects.exclude(unit="").values_list("unit", flat=True))
    for unit in sorted(in_use):
        if unit.lower() in seen:
            continue
        UnitOfMeasure.objects.get_or_create(name=unit)
        seen.add(unit.lower())


class Migration(migrations.Migration):
    dependencies = [
        ("setup", "0013_unitofmeasure"),
        ("assets", "0026_reopen_copied_route_decisions"),
        ("inventory", "0015_stockmovement_opening"),
    ]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
