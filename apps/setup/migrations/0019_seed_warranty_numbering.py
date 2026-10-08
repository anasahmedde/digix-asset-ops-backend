"""Every warranty is numbered from its own series: client, vendor, component."""
from django.db import migrations

SCHEMES = [("client_warranty", "CLW"), ("vendor_warranty", "VNW"), ("component_warranty", "CPW")]


def seed(apps, schema_editor):
    NumberingScheme = apps.get_model("setup", "NumberingScheme")
    for entity, prefix in SCHEMES:
        NumberingScheme.objects.get_or_create(
            entity=entity,
            defaults={"prefix": prefix, "separator": "-", "include_year": True, "padding": 5,
                      "next_number": 1, "is_active": True},
        )


def unseed(apps, schema_editor):
    NumberingScheme = apps.get_model("setup", "NumberingScheme")
    NumberingScheme.objects.filter(entity__in=[e for e, _ in SCHEMES]).delete()


class Migration(migrations.Migration):
    dependencies = [("setup", "0018_warranty_number_series")]
    operations = [migrations.RunPython(seed, unseed)]
