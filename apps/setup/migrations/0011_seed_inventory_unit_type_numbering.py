from django.db import migrations


def seed(apps, schema_editor):
    NumberingScheme = apps.get_model("setup", "NumberingScheme")
    NumberingScheme.objects.get_or_create(
        entity="inventory_unit_type",
        defaults={
            "prefix": "IVT",
            "separator": "-",
            "include_year": True,
            "padding": 5,
            "next_number": 1,
            "is_active": True,
        },
    )


def unseed(apps, schema_editor):
    NumberingScheme = apps.get_model("setup", "NumberingScheme")
    NumberingScheme.objects.filter(entity="inventory_unit_type").delete()


class Migration(migrations.Migration):
    dependencies = [("setup", "0010_seed_inventory_unit_numbering")]
    operations = [migrations.RunPython(seed, unseed)]
