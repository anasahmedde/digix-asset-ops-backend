"""Warranty claims are numbered WCL-<year>-<n>, like every other document."""
from django.db import migrations


def seed(apps, schema_editor):
    NumberingScheme = apps.get_model("setup", "NumberingScheme")
    NumberingScheme.objects.get_or_create(
        entity="warranty_claim",
        defaults={"prefix": "WCL", "separator": "-", "include_year": True, "padding": 5,
                  "next_number": 1, "is_active": True},
    )


def unseed(apps, schema_editor):
    apps.get_model("setup", "NumberingScheme").objects.filter(entity="warranty_claim").delete()


class Migration(migrations.Migration):
    dependencies = [("setup", "0020_warranty_claim_numbering")]
    operations = [migrations.RunPython(seed, unseed)]
